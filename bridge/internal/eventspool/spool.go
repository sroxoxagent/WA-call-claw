package eventspool

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// CallEndedEvent is the structured event written to the spool when a call ends.
type CallEndedEvent struct {
	Event          string  `json:"event"`
	CallID         string  `json:"call_id"`
	StartedAt      string  `json:"started_at"`
	EndedAt        string  `json:"ended_at"`
	DurationMs     int64   `json:"duration_ms"`
	RemoteJID      string  `json:"remote_jid"`
	Direction      string  `json:"direction,omitempty"` // "incoming" | "outgoing"
	Outcome        string  `json:"outcome,omitempty"`   // outgoing only: completed | no-answer | busy | failed
	TargetPhone    string  `json:"target_phone,omitempty"`
	AutoAnswer     bool    `json:"auto_answer"`
	PlaybackStatus string  `json:"playback_status"`
	PlaybackFile   string  `json:"playback_file,omitempty"`
	FrameCount     uint64  `json:"frame_count"`
	PCMRMSLevel    float64 `json:"pcm_rms_level"`
	RecordingFile  string  `json:"reading_file"`
	RecordingPath  string  `json:"recording_path"`
	RecordingSize  int64   `json:"recording_size_bytes"`
}

// Call directions.
const (
	DirectionIncoming = "incoming"
	DirectionOutgoing = "outgoing"
)

// Outgoing outcomes.
const (
	OutcomeCompleted = "completed"
	OutcomeNoAnswer  = "no-answer"
	OutcomeBusy      = "busy"
	OutcomeFailed    = "failed"
)

// Spool writes structured call events to a directory for the OpenClaw bridge to pick up.
type Spool struct {
	dir string
}

// NewSpool creates a spool writer rooted at dir. Creates the directory if needed.
func NewSpool(dir string) (*Spool, error) {
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("create spool dir: %w", err)
	}
	return &Spool{dir: dir}, nil
}

// WriteEvent writes a call-ended event to the spool directory.
// The filename includes the call ID for uniqueness: call-ended-{callID}.json
func (s *Spool) WriteEvent(evt *CallEndedEvent) error {
	data, err := json.MarshalIndent(evt, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}

	filename := fmt.Sprintf("call-ended-%s.json", evt.CallID)
	path := filepath.Join(s.dir, filename)

	if err := os.WriteFile(path, data, 0644); err != nil {
		return fmt.Errorf("write event: %w", err)
	}
	return nil
}

// Dir returns the spool directory path.
func (s *Spool) Dir() string {
	return s.dir
}

// NewCallEndedEvent creates a CallEndedEvent from call metadata and recording info.
func NewCallEndedEvent(callID, remoteJID, startedAt string, durationMs int64,
	autoAnswer bool, playbackStatus, playbackFile string,
	frameCount uint64, pcmRMS float64,
	recordingFile, recordingPath string, recordingSize int64,
	direction string) *CallEndedEvent {

	return &CallEndedEvent{
		Event:          "call_ended",
		CallID:         callID,
		StartedAt:      startedAt,
		EndedAt:        time.Now().Format(time.RFC3339),
		DurationMs:     durationMs,
		RemoteJID:      remoteJID,
		Direction:      direction,
		AutoAnswer:     autoAnswer,
		PlaybackStatus: playbackStatus,
		PlaybackFile:   playbackFile,
		FrameCount:     frameCount,
		PCMRMSLevel:    pcmRMS,
		RecordingFile:  recordingFile,
		RecordingPath:  recordingPath,
		RecordingSize:  recordingSize,
	}
}
