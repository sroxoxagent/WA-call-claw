package metadata

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewCallMetadata(t *testing.T) {
	audioFile := "incoming-nohpcaller-20260813-203900.wav"
	meta := NewCallMetadata("test-123", "6281234567890@s.whatsapp.net", audioFile)
	if meta.CallID != "test-123" {
		t.Errorf("expected CallID=test-123, got %s", meta.CallID)
	}
	if meta.Direction != "incoming" {
		t.Errorf("expected Direction=incoming, got %s", meta.Direction)
	}
	if meta.RemoteJID != "6281234567890@s.whatsapp.net" {
		t.Errorf("unexpected RemoteJID: %s", meta.RemoteJID)
	}
	if meta.Format != "wav" {
		t.Errorf("expected Format=wav, got %s", meta.Format)
	}
	if meta.Status != "recording" {
		t.Errorf("expected Status=recording, got %s", meta.Status)
	}
	if meta.AudioFile != audioFile {
		t.Errorf("expected AudioFile=%s, got %s", audioFile, meta.AudioFile)
	}
}

func TestSetCompleted(t *testing.T) {
	meta := NewCallMetadata("test-456", "6289999999999@s.whatsapp.net", "incoming-nohpcaller-20260813-203900.wav")
	meta.SetCompleted(15000)
	if meta.Status != "completed" {
		t.Errorf("expected Status=completed, got %s", meta.Status)
	}
	if meta.EndedAt == "" {
		t.Error("expected EndedAt to be set")
	}
	if meta.DurationMs != 15000 {
		t.Errorf("expected DurationMs=15000, got %d", meta.DurationMs)
	}
}

func TestSetFailed(t *testing.T) {
	meta := NewCallMetadata("test-789", "6287777777777@s.whatsapp.net", "incoming-nohpcaller-20260813-203900.wav")
	meta.SetFailed("connection lost")
	if meta.Status != "failed" {
		t.Errorf("expected Status=failed, got %s", meta.Status)
	}
	if meta.Error != "connection lost" {
		t.Errorf("expected Error='connection lost', got '%s'", meta.Error)
	}
}

func TestWriteMetadata(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)

	audioFile := "incoming-nohpcaller-20260813-203900.wav"
	meta := NewCallMetadata("test-write-001", "6285555555555@s.whatsapp.net", audioFile)
	meta.SetCompleted(30000)

	err := w.WriteMetadata("test-write-001", meta)
	if err != nil {
		t.Fatalf("WriteMetadata failed: %v", err)
	}

	path := filepath.Join(dir, "test-write-001", "metadata.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read metadata.json: %v", err)
	}

	dataStr := string(data)
	if !strings.Contains(dataStr, `"call_id": "test-write-001"`) {
		t.Errorf("metadata.json missing call_id")
	}
	if !strings.Contains(dataStr, `"audio_file": "incoming-nohpcaller-20260813-203900.wav"`) {
		t.Errorf("metadata.json missing expected audio_file, got:\n%s", dataStr)
	}
}
