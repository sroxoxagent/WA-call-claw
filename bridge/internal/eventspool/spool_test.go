package eventspool

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestNewSpool(t *testing.T) {
	dir := t.TempDir()
	spool, err := NewSpool(dir)
	if err != nil {
		t.Fatalf("NewSpool failed: %v", err)
	}
	if spool.Dir() != dir {
		t.Errorf("Dir() = %s, want %s", spool.Dir(), dir)
	}
}

func TestWriteEvent(t *testing.T) {
	dir := t.TempDir()
	spool, err := NewSpool(dir)
	if err != nil {
		t.Fatalf("NewSpool failed: %v", err)
	}

	evt := &CallEndedEvent{
		Event:          "call_ended",
		CallID:         "test-call-001",
		StartedAt:      "2026-08-13T20:00:00+07:00",
		EndedAt:        "2026-08-13T20:00:05+07:00",
		DurationMs:     5000,
		RemoteJID:      "6281234567890@s.whatsapp.net",
		AutoAnswer:     true,
		PlaybackStatus: "completed",
		PlaybackFile:   "playback.mp3",
		FrameCount:     83,
		PCMRMSLevel:    0.000123,
		RecordingFile:  "incoming-nohpcaller-20260813-200000.wav",
		RecordingPath:  "/opt/wa-call-claw/smoke/calls/test-call-001/incoming-nohpcaller-20260813-200000.wav",
		RecordingSize:  12345,
	}

	if err := spool.WriteEvent(evt); err != nil {
		t.Fatalf("WriteEvent failed: %v", err)
	}

	// Verify file exists
	path := filepath.Join(dir, "call-ended-test-call-001.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile failed: %v", err)
	}

	// Verify JSON is valid
	var parsed CallEndedEvent
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("JSON parse failed: %v", err)
	}

	// Verify fields
	if parsed.Event != "call_ended" {
		t.Errorf("Event = %s, want call_ended", parsed.Event)
	}
	if parsed.CallID != "test-call-001" {
		t.Errorf("CallID = %s, want test-call-001", parsed.CallID)
	}
	if parsed.DurationMs != 5000 {
		t.Errorf("DurationMs = %d, want 5000", parsed.DurationMs)
	}
	if !parsed.AutoAnswer {
		t.Error("AutoAnswer = false, want true")
	}
	if parsed.PlaybackStatus != "completed" {
		t.Errorf("PlaybackStatus = %s, want completed", parsed.PlaybackStatus)
	}
	if parsed.FrameCount != 83 {
		t.Errorf("FrameCount = %d, want 83", parsed.FrameCount)
	}
	if parsed.RecordingFile != "incoming-nohpcaller-20260813-200000.wav" {
		t.Errorf("RecordingFile = %s, want incoming-nohpcaller-20260813-200000.wav", parsed.RecordingFile)
	}
}

func TestNewCallEndedEvent(t *testing.T) {
	evt := NewCallEndedEvent(
		"call-123",
		"6289999999999@s.whatsapp.net",
		"2026-08-13T20:00:00+07:00",
		10000,
		true,
		"completed",
		"playback.mp3",
		166,
		0.000456,
		"incoming-nohpcaller-20260813-200000.wav",
		"/path/to/recording.wav",
		9999,
		DirectionIncoming,
	)

	if evt.Event != "call_ended" {
		t.Errorf("Event = %s, want call_ended", evt.Event)
	}
	if evt.CallID != "call-123" {
		t.Errorf("CallID = %s, want call-123", evt.CallID)
	}
	if evt.EndedAt == "" {
		t.Error("EndedAt should be set")
	}
	if evt.Direction != "incoming" {
		t.Errorf("Direction = %s, want incoming", evt.Direction)
	}
}

func TestWriteEventUniqueFilenames(t *testing.T) {
	dir := t.TempDir()
	spool, err := NewSpool(dir)
	if err != nil {
		t.Fatalf("NewSpool failed: %v", err)
	}

	evt1 := &CallEndedEvent{Event: "call_ended", CallID: "call-a"}
	evt2 := &CallEndedEvent{Event: "call_ended", CallID: "call-b"}

	if err := spool.WriteEvent(evt1); err != nil {
		t.Fatalf("WriteEvent 1 failed: %v", err)
	}
	if err := spool.WriteEvent(evt2); err != nil {
		t.Fatalf("WriteEvent 2 failed: %v", err)
	}

	// Both files should exist
	if _, err := os.Stat(filepath.Join(dir, "call-ended-call-a.json")); err != nil {
		t.Error("call-ended-call-a.json not found")
	}
	if _, err := os.Stat(filepath.Join(dir, "call-ended-call-b.json")); err != nil {
		t.Error("call-ended-call-b.json not found")
	}
}
