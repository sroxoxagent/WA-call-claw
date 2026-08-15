// MEOWcaller POC — minimal native WhatsApp call auto-answer + WAV recorder.
//
// This service:
//   - Connects to WhatsApp via whatsmeow + meowcaller
//   - Listens for incoming calls
//   - Auto-answers and records peer audio to WAV
//   - Saves metadata (JSON) alongside each recording
//   - Optionally bridges audio to an external agent via WebSocket
//
// Scope (locked): incoming only, auto-answer, WAV recording, no Docker/outgoing.
//
// Usage:
//
//	go run ./cmd/meowcaller-poc [-config config.yaml]
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/meowcaller-poc/internal/bridge"
	"github.com/meowcaller-poc/internal/config"
	"github.com/meowcaller-poc/internal/eventspool"
	"github.com/meowcaller-poc/internal/incoming"
	"github.com/meowcaller-poc/internal/metadata"
	"github.com/meowcaller-poc/internal/outgoing"
	"github.com/meowcaller-poc/internal/recorder"
	"github.com/meowcaller-poc/internal/storage"
	"github.com/meowcaller-poc/internal/telemetry"
	"github.com/meowcaller-poc/internal/whatsapp"
	"github.com/purpshell/meowcaller"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

func main() {
	configPath := flag.String("config", "", "path to YAML config file")
	flag.Parse()

	// Load configuration.
	cfg, err := config.LoadConfig(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	// Setup logging.
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	log.SetPrefix("[meowcaller] ")

	// Initialize WhatsApp session.
	session, err := whatsapp.NewSession(cfg.WhatsApp.StorePath)
	if err != nil {
		log.Fatalf("create session: %v", err)
	}
	defer session.Close()

	// Metadata writer.
	metaWriter := metadata.NewWriter(cfg.Recording.BaseDir)

	// Event spool for OpenClaw bridge (writes structured events for Telegram notification).
	eventSpoolDir := filepath.Join(filepath.Dir(cfg.Recording.BaseDir), "events")
	eventSpool, err := eventspool.NewSpool(eventSpoolDir)
	if err != nil {
		log.Fatalf("create event spool: %v", err)
	}
	log.Printf("event spool: %s", eventSpool.Dir())

	// Start WebSocket bridge server if enabled.
	var bridgeSrv *bridge.Server
	if cfg.Bridge.Enabled {
		bridgeSrv = bridge.NewServer(bridge.Config{
			Enabled: cfg.Bridge.Enabled,
			Listen:  cfg.Bridge.Listen,
			Path:    cfg.Bridge.Path,
		})
		go func() {
			if err := bridgeSrv.Start(context.Background()); err != nil {
				log.Printf("[bridge] server error: %v", err)
			}
		}()
		log.Printf("[bridge] WebSocket bridge enabled on %s", cfg.Bridge.Listen)
	}

	// Outgoing call manager (dial + play WAV + hang up), guarded by
	// allowlist and hourly rate limit. Exposed as POST /api/call on the bridge.
	var outgoingMgr *outgoing.Manager
	if cfg.Outgoing.Enabled {
		outgoingMgr = outgoing.NewManager(cfg.Outgoing, session.Client(), eventSpool)
		if bridgeSrv != nil {
			bridgeSrv.SetOutgoingHandler(func(req bridge.OutgoingCallMsg) bridge.OutgoingCallAck {
				callID, err := outgoingMgr.PlaceCall(req.Phone, req.Audio, req.DelayMs)
				if err != nil {
					log.Printf("[outgoing] rejected call request phone=%s: %v", req.Phone, err)
					return bridge.OutgoingCallAck{
						Type: "outgoing_call_ack", Status: "rejected", Reason: err.Error(),
					}
				}
				return bridge.OutgoingCallAck{
					Type: "outgoing_call_ack", Status: "accepted", CallID: callID,
				}
			})
			log.Printf("[outgoing] enabled: allowlist=%v session_store=%s max_calls_per_hour=%d ring_timeout=%s",
				cfg.Outgoing.Allowlist, cfg.Outgoing.SessionStorePath, cfg.Outgoing.MaxCallsPerHour, cfg.Outgoing.RingTimeout)
			log.Printf("[incoming] allowlist=%v numbers=%d",
				cfg.Incoming.Allowlist, len(cfg.Incoming.AllowlistNumbers))
		} else {
			log.Printf("[outgoing] WARNING: outgoing enabled but bridge disabled — /api/call will not be exposed")
		}
	}

	// Register the incoming-call handler before connecting so no call can arrive
	// in the small window between connection and handler registration.
	session.OnIncomingCall(func(call *meowcaller.Call) {
		handleIncomingCall(call, session.Device(), cfg, metaWriter, eventSpool, bridgeSrv)
	})

	// Connect to WhatsApp.
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	log.Println("connecting to WhatsApp...")
	if err := session.Connect(ctx); err != nil {
		log.Fatalf("connect to WhatsApp: %v", err)
	}
	log.Println("connected. Waiting for incoming calls...")

	// Block until signal.
	<-ctx.Done()
	log.Println("shutting down...")
	if bridgeSrv != nil {
		bridgeSrv.Stop()
	}
}

func resolvePeerPhone(ctx context.Context, device *whatsmeow.Client, peer types.JID) string {
	if device == nil || peer.IsEmpty() {
		return ""
	}

	if peer.Server == types.HiddenUserServer {
		phone, err := device.Store.LIDs.GetPNForLID(ctx, peer)
		if err != nil || phone.IsEmpty() {
			return ""
		}
		return phone.String()
	}

	if peer.Server == types.DefaultUserServer {
		return peer.String()
	}
	return ""
}

// handleIncomingCall processes a single incoming call.
//
// If a WebSocket agent is connected, the call audio is bridged bidirectionally:
//   - Caller → Agent: decoded PCM frames forwarded as binary WebSocket messages
//   - Agent → Caller: agent's binary PCM frames decoded and played into the call
//
// If no agent is connected, the existing fallback behavior applies:
//   - WAV recording + optional playback announcement + hangup-after-play
//
// Critical lifecycle order:
//  1. Create WAV recorder (gets the AudioSink)
//  2. Attach Receive(sink) + OnEnd BEFORE Answer()
//  3. Answer() — media loop starts, immediately finds the sink
//
// If Receive is called AFTER Answer, the media loop may start writing frames
// before the sink is attached, causing silent frame drops (empty WAV).
func handleIncomingCall(call *meowcaller.Call, device *whatsmeow.Client, cfg *config.Config, metaWriter *metadata.Writer, eventSpool *eventspool.Spool, bridgeSrv *bridge.Server) {
	callID := storage.NewCallID()
	peerJID := call.Peer().String()
	peerPhone := resolvePeerPhone(context.Background(), device, call.Peer())
	if peerPhone != "" {
		log.Printf("incoming call identity: remote_jid=%s remote_phone=%s", peerJID, peerPhone)
	} else {
		log.Printf("incoming call identity: remote_jid=%s remote_phone=unresolved", peerJID)
	}

	// ── Incoming allowlist guard ──
	// When the caller is not allowed, the bridge stays silent: it does NOT
	// answer and does NOT reject the call. Leaving the call ringing lets
	// other devices (e.g. the owner's phone) still pick it up; rejecting
	// would kill the call for every device. No media setup happens here.
	if cfg.Incoming.Allowlist && !incoming.Allowed(cfg.Incoming.AllowlistNumbers, peerJID, peerPhone) {
		log.Printf("incoming call IGNORED (not in allowlist): id=%s peer=%s phone=%q", callID, peerJID, peerPhone)
		meta := metadata.NewCallMetadata(callID, peerJID, "")
		meta.SetFailed("ignored: caller not in allowlist")
		metaWriter.WriteMetadata(callID, meta)
		return
	}

	recFilename := storage.RecordingFilenameForJID(peerJID)
	wavPath := storage.CallPath(cfg.Recording.BaseDir, callID, recFilename)

	log.Printf("incoming call: id=%s peer=%s", callID, peerJID)

	// Create metadata entry.
	meta := metadata.NewCallMetadata(callID, peerJID, recFilename)

	// Create WAV recorder and application-boundary media telemetry.
	wavRec, err := recorder.NewWAVRecorder(wavPath)
	if err != nil {
		log.Printf("create WAV recorder failed: %v", err)
		meta.SetFailed("recorder creation failed")
		metaWriter.WriteMetadata(callID, meta)
		return
	}

	callTelemetry := telemetry.NewCallTelemetry(callID)

	// ── Check if a WebSocket agent is connected ──
	var agentConn *bridge.AgentConn
	if bridgeSrv != nil {
		agentConn = bridgeSrv.Agent()
		if agentConn != nil {
			drained := agentConn.DrainBinary()
			if drained > 0 {
				log.Printf("[bridge] drained %d stale agent audio frame(s) before call %s", drained, callID)
			}
		}
	}

	// ── Build the receive sink ──
	// If agent is connected: TeeSink(WAV, Agent) — both record and forward.
	// If no agent: WAV only.
	var receiveSink meowcaller.AudioSink
	if agentConn != nil {
		receiveSink = bridge.NewTeeSink(
			telemetry.NewTelemetrySink(wavRec.InstrumentedSink(), callTelemetry, false),
			bridge.NewAgentSink(agentConn, callID),
		)
		log.Printf("[bridge] agent connected, bridging audio for call %s", callID)
	} else {
		receiveSink = telemetry.NewTelemetrySink(wavRec.InstrumentedSink(), callTelemetry, false)
	}

	// ── PHASE 1: Attach Receive sink BEFORE Answer ──
	// This ensures the media loop finds the sink immediately when it starts.
	call.Receive(receiveSink)

	// ── PHASE 2: Register OnEnd BEFORE Answer ──
	// OnEnd must be set before Answer to avoid a race where the call ends
	// before the handler is registered.
	call.OnEnd(func(reason string) {
		log.Printf("call ended: %s reason=%s", callID, reason)
		callTelemetry.Finalize()
		callTelemetry.LogSummary()
		// Finish WAV: flush buffered frames, rewrite RIFF header with final size.
		if err := wavRec.Finish(); err != nil {
			log.Printf("finalize WAV failed: %v", err)
		}
		// Log instrumentation summary.
		wavRec.LogSummary(callID)
		// Calculate duration from WAV stats.
		durationMs := wavRec.DurationMs()
		meta.SetCompleted(durationMs)
		meta.FrameCount = wavRec.FrameCount()
		meta.PCMRMSLevel = wavRec.LastRMS()
		if err := metaWriter.WriteMetadata(callID, meta); err != nil {
			log.Printf("write metadata failed: %v", err)
		}

		// ── Event Spool: Write structured event for OpenClaw bridge ──
		recSize := int64(0)
		if info, err := os.Stat(wavPath); err == nil {
			recSize = info.Size()
		}
		playbackFile := cfg.Playback.File
		playbackStatus := "skipped"
		if cfg.Playback.File != "" {
			playbackStatus = "completed"
		}
		// If agent was active, note it in the event.
		if agentConn != nil {
			playbackStatus = "bridged"
			playbackFile = "websocket_agent"
			if err := agentConn.SendJSON(bridge.CallEndMsg{
				Type: "call_ended", CallID: callID, Reason: reason, EndedAt: time.Now(),
			}); err != nil {
				log.Printf("[bridge] failed to send call_ended for %s: %v", callID, err)
			}
		}
		evt := eventspool.NewCallEndedEvent(
			callID, peerJID, meta.StartedAt, durationMs,
			true, playbackStatus, playbackFile,
			wavRec.FrameCount(), wavRec.LastRMS(),
			recFilename, wavPath, recSize,
			eventspool.DirectionIncoming,
		)
		if err := eventSpool.WriteEvent(evt); err != nil {
			log.Printf("write event spool failed: %v", err)
		} else {
			log.Printf("event spool: wrote call-ended-%s.json", callID)
		}
	})

	// ── PHASE 3: Register OnReady for logging ──
	call.OnReady(func() {
		callTelemetry.RecordRelaySetup()
		log.Printf("media ready (audio flowing): %s", callID)
	})

	// ── PHASE 4: Answer the call ──
	// Media loop starts here. Receive sink is already attached.
	if err := call.Answer(); err != nil {
		log.Printf("answer failed: %v", err)
		if finishErr := wavRec.Finish(); finishErr != nil {
			log.Printf("finalize failed recording: %v", finishErr)
		}
		meta.SetFailed("answer failed: " + err.Error())
		metaWriter.WriteMetadata(callID, meta)
		return
	}
	log.Printf("call answered: %s", callID)

	// ── PHASE 5: Agent bridging or fallback playback ──
	if agentConn != nil {
		// ── Agent connected: send metadata and start bidirectional bridging ──
		startMsg := bridge.CallStartMsg{
			Type:        "call_started",
			CallID:      callID,
			CallerID:    peerJID,
			RemotePhone: peerPhone,
			StartedAt:   time.Now(),
			Audio:       bridge.DefaultAudioFormat(),
		}
		if err := agentConn.SendJSON(startMsg); err != nil {
			log.Printf("[bridge] failed to send call_started to agent: %v", err)
			// Agent disconnected during setup — fall through to fallback.
			agentConn = nil
		} else {
			log.Printf("[bridge] sent call_started to agent: call_id=%s", callID)

			// Create AgentSource to read agent's audio frames and play into the call.
			// Keep the Player active for the entire call. AgentSource returns a nil
			// frame while its queue is empty, and the media loop converts that to
			// codec-native inactive MLow media. This lets queued PCM drain naturally.
			// We must not pause on audio_done: that message means the agent finished
			// enqueueing PCM, not that the caller has heard the queued audio yet.
			agentSrc := bridge.NewAgentSource(agentConn, callID)
			player := call.Play(agentSrc)

			// Barge-in: when the agent detects the caller speaking over bot audio,
			// it sends audio_stop. Discard all queued bot audio so the caller
			// stops hearing the bot immediately (instead of draining the buffer).
			// The next audio_playing (new TTS turn) clears the flush state so
			// fresh audio plays normally.
			//
			// NOTE: control routing is done by the single connection-wide
			// dispatcher in bridge.Server (SetAgentSource/ClearAgentSource).
			// A per-call `for ctrl := range agentConn.Controls()` goroutine
			// would never exit (the controls channel is per-connection) and
			// would race with dispatchers of later calls, silently stealing
			// their audio_playing/audio_stop messages.
			bridgeSrv.SetAgentSource(callID, agentSrc)
			defer bridgeSrv.ClearAgentSource(callID)

			player.OnFinish(func() {
				log.Printf("[bridge] agent audio source finished for call %s", callID)
				// Agent stopped sending — hang up.
				if err := call.Hangup(); err != nil {
					log.Printf("[bridge] hangup after agent source end failed: %v", err)
				}
			})

			// If agent sends hangup, end the call.
			go func() {
				select {
				case <-agentConn.Done():
					if agentConn.HangupRequested() {
						log.Printf("[bridge] agent hangup requested for call %s", callID)
						if err := call.Hangup(); err != nil {
							log.Printf("[bridge] hangup failed: %v", err)
						}
					}
				}
			}()
		}
	}

	// ── Fallback playback (no agent, or agent setup failed) ──
	// If no agent is connected (or agent setup failed above), play the
	// configured announcement WAV to the caller.
	if agentConn == nil && cfg.Playback.File != "" {
		if err := playFile(call, cfg.Playback.File, callID, cfg.Playback.HangupAfterPlay); err != nil {
			log.Printf("playback setup failed for %s: %v", callID, err)
			// Non-fatal: call is still active, caller can speak.
		}
	}

	// Enforce max duration as a safety net.
	if cfg.Recording.MaxCallDuration > 0 {
		go func() {
			time.AfterFunc(cfg.Recording.MaxCallDuration, func() {
				log.Printf("max call duration reached for %s, hanging up", callID)
				if err := call.Hangup(); err != nil {
					log.Printf("hangup failed: %v", err)
				}
			})
		}()
	}
}

// playFile loads the configured WAV announcement and plays it into the call
// with a leading silence delay. It registers OnFinish on the returned Player
// so the caller can be notified or hung up after playback completes.
func playFile(call *meowcaller.Call, path, callID string, hangupAfter bool) error {
	wavSrc, err := meowcaller.WAVFile(path)
	if err != nil {
		return fmt.Errorf("open WAV %s: %w", path, err)
	}

	// Wrap with 2-second silence before playback starts.
	src := recorder.NewSilenceThenSource(wavSrc, recorder.SilenceDurationMs)

	player := call.Play(src)
	log.Printf("playback started: %s file=%s delay=%dms", callID, path, recorder.SilenceDurationMs)

	player.OnFinish(func() {
		log.Printf("playback finished: %s", callID)
		if hangupAfter {
			log.Printf("hangup_after_play: hanging up %s", callID)
			if err := call.Hangup(); err != nil {
				log.Printf("hangup after playback failed: %v", err)
			}
		}
	})

	return nil
}
