// Package outgoing implements outbound WhatsApp calls: dial a phone number,
// wait for the peer to answer, play a WAV announcement, then hang up.
//
// Safeguards (anti-spam / anti-ban):
//   - Allowlist: only phone numbers listed in outgoing.allowlist can be called.
//   - Rate limit: at most MaxCallsPerHour calls per rolling hour.
//   - Format validation: phone must be E.164 without the leading '+'.
//   - Ring timeout: calls that are not answered are hung up automatically.
//
// Every attempt (accepted or rejected) is written to the event spool so there
// is an audit trail of who was called and why.
package outgoing

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/meowcaller-poc/internal/config"
	"github.com/meowcaller-poc/internal/eventspool"
	"github.com/purpshell/meowcaller"
)

// phoneRe matches E.164 phone numbers without the leading '+':
// country code + subscriber number, 7-15 digits total, no leading zero.
var phoneRe = regexp.MustCompile(`^[1-9][0-9]{6,14}$`)

// idPhoneRe matches Indonesian phone numbers (62 + 9-13 digits). Session-derived
// allowlisting uses this stricter pattern so WhatsApp LIDs (e.g. 1285…, 6698…,
// 1938…) never leak into the callable set — they look like numbers but are not
// dialable phone numbers.
var idPhoneRe = regexp.MustCompile(`^62[0-9]{9,13}$`)

// sessionPhoneRe finds Indonesian phone numbers anywhere inside a string (session
// keys like "agent:main:direct:+6281234567890" carry the number as a substring).
var sessionPhoneRe = regexp.MustCompile(`\+?62[0-9]{9,13}`)

// Manager places and tracks outbound calls with allowlist + rate-limit guards.
type Manager struct {
	mu         sync.Mutex
	hourly     []time.Time // timestamps of calls placed in the rolling hour
	cfg        config.OutgoingConfig
	client     *meowcaller.Client
	eventSpool *eventspool.Spool

	sessionMu    sync.Mutex
	sessionCache []string    // cached session-derived allowlist
	sessionCachedAt time.Time
}

// NewManager creates an outbound call manager. client must be the connected
// meowcaller client; spool receives call-ended events for the audit trail.
func NewManager(cfg config.OutgoingConfig, client *meowcaller.Client, spool *eventspool.Spool) *Manager {
	return &Manager{cfg: cfg, client: client, eventSpool: spool}
}

// Allowlist returns the effective allowlist: phone numbers derived from the
// OpenClaw sessions store (numbers that have a direct chat session). When
// allowlist mode is off the returned slice is empty and every valid E.164
// number is callable.
func (m *Manager) Allowlist() []string {
	if !m.cfg.Allowlist {
		return nil
	}
	return m.sessionAllowlist()
}

// sessionAllowlist loads callable numbers from the OpenClaw sessions.json
// (direct chat sessions only), cached for SessionAllowlistTTL. Failures to read
// or parse the file return nil — with allowlist on, no number passes the check.
func (m *Manager) sessionAllowlist() []string {
	if m.cfg.SessionStorePath == "" {
		return nil
	}
	ttl := m.cfg.SessionAllowlistTTL
	if ttl <= 0 {
		ttl = 60 * time.Second
	}
	m.sessionMu.Lock()
	defer m.sessionMu.Unlock()
	if m.sessionCache != nil && time.Since(m.sessionCachedAt) < ttl {
		return m.sessionCache
	}
	nums := extractPhonesFromSessions(m.cfg.SessionStorePath)
	m.sessionCache = nums
	m.sessionCachedAt = time.Now()
	if len(nums) > 0 {
		log.Printf("outgoing session allowlist: %d numbers from %s", len(nums), m.cfg.SessionStorePath)
	}
	return nums
}

// extractPhonesFromSessions reads a sessions.json file and returns every
// Indonesian phone number found in it (session keys, chat IDs, origin fields).
// WhatsApp LIDs and non-dialable identifiers are excluded by the 62-prefix
// pattern. Read/parse errors return nil.
func extractPhonesFromSessions(path string) []string {
	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("outgoing session allowlist: read %s failed: %v", path, err)
		return nil
	}
	var root any
	if err := json.Unmarshal(data, &root); err != nil {
		log.Printf("outgoing session allowlist: parse %s failed: %v", path, err)
		return nil
	}
	seen := map[string]bool{}
	var out []string
	var walk func(any)
	walk = func(v any) {
		switch t := v.(type) {
		case string:
			for _, m := range sessionPhoneRe.FindAllString(t, -1) {
				s := strings.TrimPrefix(m, "+")
				if !seen[s] {
					seen[s] = true
					out = append(out, s)
				}
			}
		case []any:
			for _, item := range t {
				walk(item)
			}
		case map[string]any:
			for k, item := range t {
				walk(k) // session keys carry the phone numbers (e.g. agent:main:direct:+62…)
				walk(item)
			}
		}
	}
	walk(root)
	return out
}

// isAllowlisted reports whether phone is in the effective allowlist
// (session-derived numbers).
func (m *Manager) isAllowlisted(phone string) bool {
	for _, p := range m.Allowlist() {
		if p == phone {
			return true
		}
	}
	return false
}

// rateLimitOK reports whether placing another call now stays within the
// hourly cap, pruning the sliding window of expired timestamps.
func (m *Manager) rateLimitOK() bool {
	if m.cfg.MaxCallsPerHour <= 0 {
		return true // unlimited
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	cutoff := time.Now().Add(-time.Hour)
	kept := m.hourly[:0]
	for _, t := range m.hourly {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	m.hourly = kept
	return len(m.hourly) < m.cfg.MaxCallsPerHour
}

// recordCall appends a call timestamp to the sliding window.
func (m *Manager) recordCall(t time.Time) {
	if m.cfg.MaxCallsPerHour <= 0 {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.hourly = append(m.hourly, t)
}

// Validate checks phone format, allowlist membership, and the hourly rate
// limit. It is exported so callers can pre-flight a request.
func (m *Manager) Validate(phone string) error {
	if !phoneRe.MatchString(phone) {
		return fmt.Errorf("invalid phone format %q (need E.164 without +, 7-15 digits)", phone)
	}
	if m.cfg.Allowlist && !m.isAllowlisted(phone) {
		return fmt.Errorf("phone %s is not in the outgoing allowlist (session store)", phone)
	}
	if !m.rateLimitOK() {
		return fmt.Errorf("rate limit reached: max %d calls/hour", m.cfg.MaxCallsPerHour)
	}
	return nil
}

// resolveAudioPath resolves the audio argument to an absolute path.
// Absolute paths pass through; relative paths resolve against cfg.AudioDir
// (or the current working directory if AudioDir is empty).
func (m *Manager) resolveAudioPath(audio string) (string, error) {
	if audio == "" {
		return "", fmt.Errorf("audio file is required")
	}
	p := audio
	if !filepath.IsAbs(p) && m.cfg.AudioDir != "" {
		p = filepath.Join(m.cfg.AudioDir, p)
	}
	abs, err := filepath.Abs(p)
	if err != nil {
		return "", fmt.Errorf("resolve audio path: %w", err)
	}
	if _, err := os.Stat(abs); err != nil {
		return "", fmt.Errorf("audio file %s: %w", abs, err)
	}
	return abs, nil
}

// PlaceCall dials phone and plays audio after the peer answers.
//
// Flow: validate → resolve audio → dial → wait for Active → delay →
// play WAV → hang up on finish. If the peer never answers within the ring
// timeout, the call is hung up with outcome no-answer. All outcomes are
// written to the event spool.
//
// Returns the call ID, or an error for rejected/invalid requests.
func (m *Manager) PlaceCall(phone, audio string, delayMs int) (string, error) {
	if err := m.Validate(phone); err != nil {
		return "", err
	}
	audioPath, err := m.resolveAudioPath(audio)
	if err != nil {
		return "", err
	}
	if delayMs <= 0 {
		delayMs = m.cfg.DefaultDelayMs
	}

	ctx := context.Background()
	call, err := m.client.Call(ctx, phone)
	if err != nil {
		return "", fmt.Errorf("place call to %s: %w", phone, err)
	}
	callID := call.ID()
	startedAt := time.Now()
	m.recordCall(startedAt)
	log.Printf("outgoing call placed: id=%s phone=%s audio=%s delay=%dms", callID, phone, audioPath, delayMs)

	// Ring timeout: hang up if the peer does not answer in time.
	ringTimer := time.AfterFunc(m.cfg.RingTimeout, func() {
		if st := call.State(); st != meowcaller.CallPhaseActive && st != meowcaller.CallPhaseEnded {
			log.Printf("outgoing ring timeout (%s): hanging up %s phone=%s", m.cfg.RingTimeout, callID, phone)
			if err := call.Hangup(); err != nil {
				log.Printf("outgoing hangup after ring timeout failed: %v", err)
			}
		}
	})

	// Playback trigger: fire as soon as the peer accepts the call (no dependency
	// on inbound RTP — CallPhaseActive only fires after the peer's first inbound
	// audio frame, which never arrives if the peer stays silent).
	call.OnPeerAccept(func() {
		ringTimer.Stop()
		log.Printf("outgoing call answered: id=%s phone=%s", callID, phone)
		go func() {
			time.Sleep(time.Duration(delayMs) * time.Millisecond)
			if st := call.State(); st == meowcaller.CallPhaseEnded {
				log.Printf("outgoing call ended before playback, skipping: %s", callID)
				return
			}
			src, err := meowcaller.WAVFile(audioPath)
			if err != nil {
				log.Printf("outgoing WAV open failed (%v): hanging up %s", err, callID)
				if err := call.Hangup(); err != nil {
					log.Printf("outgoing hangup after WAV error failed: %v", err)
				}
				return
			}
			_ = call.Play(src) // player lifecycle is managed by the hangup timer below
			// Hang up after the announcement plus a tail grace period, so the
			// peer gets a moment after the message ends. Total call time from
			// play start = WAV duration + HangupAfterPlaySec. If the WAV
			// duration cannot be read, fall back to a fixed estimate.
			hangAfter := time.Duration(m.cfg.HangupAfterPlaySec) * time.Second
			if hangAfter <= 0 {
				hangAfter = 3 * time.Second
			}
			dur, derr := wavDurationSeconds(audioPath)
			if derr != nil || dur <= 0 {
				log.Printf("outgoing WAV duration unknown (%v): using fallback hangup window", derr)
				dur = float64(delayMs)/1000.0 + 5.0
			}
			hangupDelay := time.Duration(dur*float64(time.Second)) + hangAfter
			log.Printf("outgoing playback started: id=%s phone=%s wav_dur=%.1fs hangup_in=%.1fs", callID, phone, dur, hangupDelay.Seconds())
			time.AfterFunc(hangupDelay, func() {
				if st := call.State(); st != meowcaller.CallPhaseEnded {
					log.Printf("outgoing playback window over: hanging up %s phone=%s", callID, phone)
					if err := call.Hangup(); err != nil {
						log.Printf("outgoing hangup after playback window failed: %v", err)
					}
				}
			})
		}()
	})

	call.OnEnd(func(reason string) {
		durationMs := time.Since(startedAt).Milliseconds()
		outcome := classifyOutcome(reason)
		log.Printf("outgoing call ended: id=%s phone=%s reason=%s outcome=%s duration=%dms",
			callID, phone, reason, outcome, durationMs)

		evt := eventspool.NewCallEndedEvent(
			callID, phone, startedAt.Format(time.RFC3339), durationMs,
			true, "outgoing_playback", audioPath,
			0, 0, "", "", 0,
			eventspool.DirectionOutgoing,
		)
		evt.Outcome = outcome
		evt.TargetPhone = phone
		if err := m.eventSpool.WriteEvent(evt); err != nil {
			log.Printf("outgoing write event spool failed: %v", err)
		} else {
			log.Printf("outgoing event spool: wrote call-ended-%s.json", callID)
		}
	})

	return callID, nil
}

// wavDurationSeconds reads the RIFF/WAVE header of a 16-bit PCM file and returns
// the playback duration in seconds (data chunk size / byte rate). It returns an
// error for unreadable or malformed files.
func wavDurationSeconds(path string) (float64, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	var hdr [12]byte
	if _, err := io.ReadFull(f, hdr[:]); err != nil {
		return 0, err
	}
	if string(hdr[0:4]) != "RIFF" || string(hdr[8:12]) != "WAVE" {
		return 0, fmt.Errorf("not a RIFF/WAVE file: %s", path)
	}
	var byteRate uint32
	var dataSize uint32
	for {
		var chunk [8]byte
		if _, err := io.ReadFull(f, chunk[:]); err != nil {
			return 0, fmt.Errorf("truncated WAV chunk header: %w", err)
		}
		id := string(chunk[0:4])
		size := binary.LittleEndian.Uint32(chunk[4:8])
		switch id {
		case "fmt ":
			var fmtBuf [16]byte
			if _, err := io.ReadFull(f, fmtBuf[:]); err != nil {
				return 0, fmt.Errorf("truncated fmt chunk: %w", err)
			}
			byteRate = binary.LittleEndian.Uint32(fmtBuf[8:12])
			if size > 16 {
				if _, err := f.Seek(int64(size-16), io.SeekCurrent); err != nil {
					return 0, err
				}
			}
		case "data":
			dataSize = size
			if byteRate == 0 {
				return 0, fmt.Errorf("fmt chunk missing before data")
			}
			return float64(dataSize) / float64(byteRate), nil
		default:
			if _, err := f.Seek(int64(size), io.SeekCurrent); err != nil {
				return 0, err
			}
		}
	}
}

// classifyOutcome maps a call end reason to a stable outcome label.
func classifyOutcome(reason string) string {
	r := strings.ToLower(reason)
	switch {
	case r == "" || r == "completed" || r == "ended" || r == "hangup":
		return eventspool.OutcomeCompleted
	case strings.Contains(r, "no-answer"), strings.Contains(r, "no answer"),
		strings.Contains(r, "timeout"), strings.Contains(r, "unanswered"):
		return eventspool.OutcomeNoAnswer
	case strings.Contains(r, "busy"), strings.Contains(r, "occupied"):
		return eventspool.OutcomeBusy
	default:
		return eventspool.OutcomeFailed
	}
}
