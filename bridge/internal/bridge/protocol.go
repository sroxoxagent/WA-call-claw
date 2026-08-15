package bridge

import "time"

// Protocol defines the WebSocket framing for the MEOWcaller audio bridge.
//
// Binary messages: raw s16le mono 16 kHz PCM audio frames.
//   - Each binary message is exactly 1920 bytes (960 samples × 2 bytes/sample).
//   - This matches meowcaller's 60 ms frame cadence (16 kHz × 0.06s = 960 samples).
//
// JSON messages: control and metadata.
//   - Direction: MEOWcaller → Agent (call metadata on answer/end)
//   - Direction: Agent → MEOWcaller (hangup command)

// AudioFormat describes the PCM audio format sent over the bridge.
type AudioFormat struct {
	SampleRate int    `json:"sample_rate"` // 16000
	Channels   int    `json:"channels"`    // 1 (mono)
	Encoding   string `json:"encoding"`    // "s16le"
	FrameSize  int    `json:"frame_size"`  // 1920 bytes (960 samples × 2)
	FrameMs    int    `json:"frame_ms"`    // 60 ms
}

// DefaultAudioFormat returns the fixed audio format for the bridge.
func DefaultAudioFormat() AudioFormat {
	return AudioFormat{
		SampleRate: 16000,
		Channels:   1,
		Encoding:   "s16le",
		FrameSize:  1920,
		FrameMs:    60,
	}
}

// CallStartMsg is sent from MEOWcaller to the agent when a call is answered
// and an agent is connected. It carries metadata the agent needs to begin
// processing the audio stream.
type CallStartMsg struct {
	Type        string      `json:"type"`                   // "call_started"
	CallID      string      `json:"call_id"`                // unique call identifier
	CallerID    string      `json:"caller_id"`              // remote JID (e.g. "120363xxx@g.us" or LID)
	RemotePhone string      `json:"remote_phone,omitempty"` // canonical phone JID resolved from the remote LID
	StartedAt   time.Time   `json:"started_at"`             // call answer timestamp (RFC3339)
	Audio       AudioFormat `json:"audio"`                  // PCM format description
}

// CallEndMsg is sent from MEOWcaller to the agent when the active call ends.
type CallEndMsg struct {
	Type    string    `json:"type"`     // "call_ended"
	CallID  string    `json:"call_id"`  // unique call identifier
	Reason  string    `json:"reason"`   // call end reason
	EndedAt time.Time `json:"ended_at"` // end timestamp (RFC3339)
}

// HangupMsg is sent from the agent to MEOWcaller to request ending the call.
type HangupMsg struct {
	Type   string `json:"type"`   // "hangup"
	Reason string `json:"reason"` // optional reason string
}

// OutgoingCallMsg requests an outbound call. It is accepted via HTTP POST
// /api/call (JSON body), not over the agent WebSocket — the agent connection
// is reserved for one voice agent, and outbound requests come from the
// wacall CLI or any local tooling.
type OutgoingCallMsg struct {
	Type    string `json:"type"`               // "outgoing_call"
	Phone   string `json:"phone"`              // E.164 without leading +
	Audio   string `json:"audio"`              // WAV path (absolute, or relative to outgoing.audio_dir)
	DelayMs int    `json:"delay_ms,omitempty"` // silence before playback in ms; 0 = config default
}

// OutgoingCallAck is the response to an OutgoingCallMsg.
// Status is "accepted" (call placed) or "rejected" (see Reason).
type OutgoingCallAck struct {
	Type   string `json:"type"`   // "outgoing_call_ack"
	Status string `json:"status"` // "accepted" | "rejected"
	CallID string `json:"call_id,omitempty"`
	Reason string `json:"reason,omitempty"`
}
