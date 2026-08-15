package storage

import (
	"regexp"
	"testing"
	"time"
)

func TestNewCallID(t *testing.T) {
	id1 := NewCallID()
	id2 := NewCallID()

	if id1 == "" {
		t.Error("expected non-empty call ID")
	}
	if id1 == id2 {
		t.Errorf("expected unique IDs, got same: %s", id1)
	}
	// Call ID should contain a timestamp prefix
	if len(id1) < 20 {
		t.Errorf("call ID too short: %s", id1)
	}
}

func TestCallPath(t *testing.T) {
	path := CallPath("/var/lib/meowcaller/calls", "20260813T185500Z-abc123", "incoming.wav")
	expected := "/var/lib/meowcaller/calls/20260813T185500Z-abc123/incoming.wav"
	if path != expected {
		t.Errorf("expected %s, got %s", expected, path)
	}
}

func TestRecordingFilenameFormat(t *testing.T) {
	// Use a known UTC time: 2026-08-13 13:39:00 UTC = 2026-08-13 20:39:00 WIB
	utcTime := time.Date(2026, 8, 13, 13, 39, 0, 0, time.UTC)
	fn := RecordingFilenameForJIDAt("999999999999998@lid", utcTime)

	expected := "incoming-999999999999998@lid-20260813-203900.wav"
	if fn != expected {
		t.Errorf("expected %s, got %s", expected, fn)
	}
}

func TestRecordingFilenameRegex(t *testing.T) {
	fn := RecordingFilename()
	// Unknown-JID fallback still has the same timestamp shape.
	re := regexp.MustCompile(`^incoming-unknown-\d{8}-\d{6}\.wav$`)
	if !re.MatchString(fn) {
		t.Errorf("filename does not match expected pattern: %s", fn)
	}
}

func TestRecordingFilenameJakartaTimezone(t *testing.T) {
	// UTC 2026-01-01 17:00:00 = WIB 2026-01-02 00:00:00 (next day)
	utcTime := time.Date(2026, 1, 1, 17, 0, 0, 0, time.UTC)
	fn := RecordingFilenameForJIDAt("999999999999998:30@lid", utcTime)

	expected := "incoming-999999999999998:30@lid-20260102-000000.wav"
	if fn != expected {
		t.Errorf("expected %s, got %s (timezone conversion may be wrong)", expected, fn)
	}
}

func TestRecordingFilenameSanitizesPathSeparators(t *testing.T) {
	fn := RecordingFilenameForJIDAt("user/../secret\x00", time.Date(2026, 8, 13, 13, 39, 0, 0, time.UTC))
	if fn != "incoming-user_.._secret_-20260813-203900.wav" {
		t.Errorf("unexpected sanitized filename: %s", fn)
	}
}

func TestRecordingFilenameUniqueness(t *testing.T) {
	// Two calls at different times must produce different filenames.
	t1 := time.Date(2026, 8, 13, 13, 39, 0, 0, time.UTC)
	t2 := time.Date(2026, 8, 13, 13, 39, 1, 0, time.UTC)
	fn1 := RecordingFilenameAt(t1)
	fn2 := RecordingFilenameAt(t2)
	if fn1 == fn2 {
		t.Errorf("expected different filenames for different times, got both: %s", fn1)
	}
}
