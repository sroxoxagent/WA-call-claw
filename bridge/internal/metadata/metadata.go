package metadata

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// CallMetadata stores information about a completed call.
type CallMetadata struct {
	CallID      string  `json:"call_id"`
	Direction   string  `json:"direction"`
	RemoteJID   string  `json:"remote_jid"`
	StartedAt   string  `json:"started_at"`
	EndedAt     string  `json:"ended_at,omitempty"`
	AudioFile   string  `json:"audio_file"`
	Format      string  `json:"format"`
	Status      string  `json:"status"`
	Error       string  `json:"error,omitempty"`
	DurationMs  int64   `json:"duration_ms,omitempty"`
	FrameCount  uint64  `json:"frame_count,omitempty"`
	PCMRMSLevel float64 `json:"pcm_rms_level,omitempty"`
}

// Writer writes metadata JSON files for calls.
type Writer struct {
	baseDir string
}

// NewWriter creates a new metadata writer rooted at baseDir.
func NewWriter(baseDir string) *Writer {
	return &Writer{baseDir: baseDir}
}

// WriteMetadata writes a metadata.json for the given call ID.
func (w *Writer) WriteMetadata(callID string, meta *CallMetadata) error {
	dir := filepath.Join(w.baseDir, callID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create call dir: %w", err)
	}

	path := filepath.Join(dir, "metadata.json")
	data, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal metadata: %w", err)
	}

	if err := os.WriteFile(path, data, 0644); err != nil {
		return fmt.Errorf("write metadata: %w", err)
	}
	return nil
}

// NewCallMetadata creates a new metadata entry with the given parameters.
// The audioFile parameter specifies the recording filename (e.g. "incoming-nohpcaller-20260813-203900.wav").
func NewCallMetadata(callID, remoteJID, audioFile string) *CallMetadata {
	return &CallMetadata{
		CallID:    callID,
		Direction: "incoming",
		RemoteJID: remoteJID,
		StartedAt: time.Now().Format(time.RFC3339),
		AudioFile: audioFile,
		Format:    "wav",
		Status:    "recording",
	}
}

// SetCompleted marks the metadata as completed with an optional end time.
func (m *CallMetadata) SetCompleted(durationMs int64) {
	m.EndedAt = time.Now().Format(time.RFC3339)
	m.Status = "completed"
	m.DurationMs = durationMs
}

// SetFailed marks the metadata as failed with an error message.
func (m *CallMetadata) SetFailed(err string) {
	m.EndedAt = time.Now().Format(time.RFC3339)
	m.Status = "failed"
	m.Error = err
}
