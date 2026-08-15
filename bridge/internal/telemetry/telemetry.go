// Package telemetry provides per-call media diagnostics at the application boundary.
//
// It tracks:
//   - Relay setup time (from call creation to first frame)
//   - Inbound PCM frame count
//   - First/last frame timestamps
//   - Sink write count and errors
//
// Limitations documented:
//   - Raw RTP/SRTP internals are NOT exposed by the meowcaller dependency.
//     All metrics are application-boundary only (WriteFrame calls into the sink).
//   - We cannot observe packet loss, jitter, or codec negotiation at this layer.
//   - Frame timestamps are recorded when WriteFrame is called, not when the
//     audio was actually captured by the microphone/relay.
//
// Usage:
//
//	telem := telemetry.NewCallTelemetry(callID)
//	telem.RecordRelaySetup()
//	// ... in sink.WriteFrame:
//	telem.RecordFrame(frame)
//	// ... on call end:
//	telem.Finalize()
//	log.Printf("telemetry: %s", telem.Summary())
package telemetry

import (
	"fmt"
	"log"
	"sync"
	"time"
)

// CallTelemetry tracks media diagnostics for a single call.
type CallTelemetry struct {
	mu sync.Mutex

	CallID string

	// Lifecycle timestamps.
	CreatedAt    time.Time
	RelaySetUpAt time.Time // when relay setup was recorded
	FirstFrameAt time.Time // when the first PCM frame was received
	LastFrameAt  time.Time // when the last PCM frame was received
	FinalizedAt  time.Time // when Finalize() was called

	// Counters.
	FrameCount  uint64 // number of inbound PCM frames received by the sink
	SinkWrites  uint64 // number of WriteFrame calls
	SinkErrors  uint64 // number of WriteFrame errors
	AgentWrites uint64 // number of outbound frames to agent (if bridged)
	AgentErrors uint64 // number of outbound frame errors

	// Computed on finalize.
	DurationMs   int64 // wall-clock duration from CreatedAt to FinalizedAt
	RelaySetupMs int64 // time from CreatedAt to RelaySetUpAt
	FirstFrameMs int64 // time from CreatedAt to FirstFrameAt (relay latency)
	StreamMs     int64 // time from FirstFrameAt to LastFrameAt (active audio stream)

	// Status.
	finalized bool
}

// NewCallTelemetry creates a new telemetry tracker for the given call.
func NewCallTelemetry(callID string) *CallTelemetry {
	return &CallTelemetry{
		CallID:    callID,
		CreatedAt: time.Now(),
	}
}

// RecordRelaySetup records when the library reports media ready. The
// dependency does not expose raw relay/RTP/SRTP stages, so this is the
// application boundary's media-ready marker, not proof of packet delivery.
func (t *CallTelemetry) RecordRelaySetup() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.RelaySetUpAt.IsZero() {
		t.RelaySetUpAt = time.Now()
	}
}

// RecordFrame records that an inbound PCM frame was received.
// Call this from the sink's WriteFrame method.
func (t *CallTelemetry) RecordFrame(frame []float32) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	t.FrameCount++
	t.SinkWrites++
	if t.FirstFrameAt.IsZero() {
		t.FirstFrameAt = now
	}
	t.LastFrameAt = now
}

// RecordFrameError records a sink write error.
func (t *CallTelemetry) RecordFrameError() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.SinkErrors++
}

// RecordAgentWrite records an outbound frame to the agent.
func (t *CallTelemetry) RecordAgentWrite() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.AgentWrites++
}

// RecordAgentError records an outbound frame error to the agent.
func (t *CallTelemetry) RecordAgentError() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.AgentErrors++
}

// Finalize computes derived metrics and marks the telemetry as complete.
func (t *CallTelemetry) Finalize() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.finalized {
		return
	}
	t.FinalizedAt = time.Now()
	t.finalized = true

	t.DurationMs = t.FinalizedAt.Sub(t.CreatedAt).Milliseconds()

	if !t.RelaySetUpAt.IsZero() {
		t.RelaySetupMs = t.RelaySetUpAt.Sub(t.CreatedAt).Milliseconds()
	}
	if !t.FirstFrameAt.IsZero() {
		t.FirstFrameMs = t.FirstFrameAt.Sub(t.CreatedAt).Milliseconds()
	}
	if !t.LastFrameAt.IsZero() && !t.FirstFrameAt.IsZero() {
		t.StreamMs = t.LastFrameAt.Sub(t.FirstFrameAt).Milliseconds()
	}
}

// Summary returns a human-readable summary string.
func (t *CallTelemetry) Summary() string {
	t.mu.Lock()
	defer t.mu.Unlock()

	return fmt.Sprintf(
		"call=%s frames=%d relay_setup_ms=%d first_frame_ms=%d stream_ms=%d "+
			"duration_ms=%d sink_writes=%d sink_errors=%d agent_writes=%d agent_errors=%d",
		t.CallID,
		t.FrameCount,
		t.RelaySetupMs,
		t.FirstFrameMs,
		t.StreamMs,
		t.DurationMs,
		t.SinkWrites,
		t.SinkErrors,
		t.AgentWrites,
		t.AgentErrors,
	)
}

// LogSummary logs the telemetry summary.
func (t *CallTelemetry) LogSummary() {
	log.Printf("[telemetry] %s", t.Summary())
}

// TelemetrySink wraps a meowcaller.AudioSink and records telemetry for each frame.
//
// This is the application-boundary instrumentation point. We observe frames as
// they cross the WriteFrame boundary, which is the earliest point we can
// measure without modifying the meowcaller library internals.
type TelemetrySink struct {
	inner   interface{ WriteFrame([]float32) error }
	telem   *CallTelemetry
	isAgent bool // true if this is the agent outbound sink
}

// NewTelemetrySink wraps inner and records telemetry to telem.
// If isAgent is true, it records agent outbound metrics instead of inbound.
func NewTelemetrySink(inner interface{ WriteFrame([]float32) error }, telem *CallTelemetry, isAgent bool) *TelemetrySink {
	return &TelemetrySink{inner: inner, telem: telem, isAgent: isAgent}
}

// WriteFrame records the frame in telemetry and delegates to the inner sink.
func (s *TelemetrySink) WriteFrame(frame []float32) error {
	if !s.isAgent {
		// call.Receive delivers decoded float32 PCM samples. The WAV writer
		// stores the same frame as signed 16-bit PCM, so log both sizes.
		log.Printf(
			"[callReceive] call=%s samples=%d float32_bytes=%d pcm16_bytes=%d",
			s.telem.CallID,
			len(frame),
			len(frame)*4,
			len(frame)*2,
		)
	}
	err := s.inner.WriteFrame(frame)
	if err != nil {
		if s.isAgent {
			s.telem.RecordAgentError()
		} else {
			s.telem.RecordFrameError()
		}
		return err
	}
	if s.isAgent {
		s.telem.RecordAgentWrite()
	} else {
		s.telem.RecordFrame(frame)
	}
	return nil
}

// Close delegates to the inner sink's Close method if available.
func (s *TelemetrySink) Close() error {
	type closer interface{ Close() error }
	if c, ok := s.inner.(closer); ok {
		return c.Close()
	}
	return nil
}
