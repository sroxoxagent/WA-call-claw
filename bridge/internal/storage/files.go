package storage

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"path/filepath"
	"strings"
	"time"
)

// jakartaLoc is the Asia/Jakarta timezone (WIB, UTC+7) for recording filenames.
var jakartaLoc = func() *time.Location {
	loc, err := time.LoadLocation("Asia/Jakarta")
	if err != nil {
		// Fallback: should never happen on Linux with tzdata installed.
		return time.FixedZone("WIB", 7*3600)
	}
	return loc
}()

// NewCallID generates a unique call ID based on timestamp + random suffix.
func NewCallID() string {
	now := time.Now().UTC().Format("20060102T150405Z")
	b := make([]byte, 4)
	rand.Read(b)
	return fmt.Sprintf("%s-%s", now, hex.EncodeToString(b))
}

// RecordingFilename generates a WAV recording filename using Asia/Jakarta local time.
// Format: incoming-{jid}-YYYYMMDD-HHmmss.wav
// The no-JID variant is retained for callers that do not have peer metadata yet.
func RecordingFilename() string {
	return RecordingFilenameForJIDAt("unknown", time.Now())
}

// RecordingFilenameAt generates a WAV recording filename at the given time in Asia/Jakarta.
// This legacy helper is retained for deterministic tests and uses an unknown JID.
func RecordingFilenameAt(t time.Time) string {
	return RecordingFilenameForJIDAt("unknown", t)
}

// RecordingFilenameForJID generates a WAV recording filename containing the peer JID.
func RecordingFilenameForJID(jid string) string {
	return RecordingFilenameForJIDAt(jid, time.Now())
}

// RecordingFilenameForJIDAt generates a deterministic JID-labelled WAV filename.
// Path separators and control characters are replaced so the JID cannot escape
// the call directory when used as a filename component.
func RecordingFilenameForJIDAt(jid string, t time.Time) string {
	jid = strings.TrimSpace(jid)
	if jid == "" {
		jid = "unknown"
	}
	jid = strings.NewReplacer("/", "_", "\\\\", "_", "\x00", "_").Replace(jid)
	return fmt.Sprintf("incoming-%s-%s.wav", jid, t.In(jakartaLoc).Format("20060102-150405"))
}

// CallPath returns the full path for a recording file within a call directory.
func CallPath(baseDir, callID, filename string) string {
	return filepath.Join(baseDir, callID, filename)
}
