package telemetry

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestNewCallTelemetry(t *testing.T) {
	before := time.Now()
	telem := NewCallTelemetry("test-call-001")
	after := time.Now()

	if telem.CallID != "test-call-001" {
		t.Errorf("expected CallID=test-call-001, got %s", telem.CallID)
	}
	if telem.CreatedAt.Before(before) || telem.CreatedAt.After(after) {
		t.Errorf("CreatedAt out of range: %v", telem.CreatedAt)
	}
	if telem.FrameCount != 0 {
		t.Errorf("expected FrameCount=0, got %d", telem.FrameCount)
	}
}

func TestRecordRelaySetup(t *testing.T) {
	telem := NewCallTelemetry("test-call-002")
	if !telem.RelaySetUpAt.IsZero() {
		t.Error("expected RelaySetUpAt to be zero initially")
	}

	telem.RecordRelaySetup()
	if telem.RelaySetUpAt.IsZero() {
		t.Error("expected RelaySetUpAt to be set after RecordRelaySetup")
	}
	if telem.RelaySetUpAt.Before(telem.CreatedAt) {
		t.Error("expected RelaySetUpAt >= CreatedAt")
	}

	// Calling again should be idempotent.
	original := telem.RelaySetUpAt
	time.Sleep(5 * time.Millisecond)
	telem.RecordRelaySetup()
	if !telem.RelaySetUpAt.Equal(original) {
		t.Error("expected RecordRelaySetup to be idempotent")
	}
}

func TestRecordFrame(t *testing.T) {
	telem := NewCallTelemetry("test-call-003")
	frame := make([]float32, 960) // 60ms at 16kHz

	telem.RecordFrame(frame)
	if telem.FrameCount != 1 {
		t.Errorf("expected FrameCount=1, got %d", telem.FrameCount)
	}
	if telem.SinkWrites != 1 {
		t.Errorf("expected SinkWrites=1, got %d", telem.SinkWrites)
	}
	if telem.FirstFrameAt.IsZero() {
		t.Error("expected FirstFrameAt to be set")
	}
	if telem.LastFrameAt.IsZero() {
		t.Error("expected LastFrameAt to be set")
	}

	telem.RecordFrame(frame)
	telem.RecordFrame(frame)
	if telem.FrameCount != 3 {
		t.Errorf("expected FrameCount=3, got %d", telem.FrameCount)
	}
	if telem.LastFrameAt.Before(telem.FirstFrameAt) {
		t.Error("expected LastFrameAt >= FirstFrameAt")
	}
}

func TestRecordFrameError(t *testing.T) {
	telem := NewCallTelemetry("test-call-004")
	telem.RecordFrameError()
	if telem.SinkErrors != 1 {
		t.Errorf("expected SinkErrors=1, got %d", telem.SinkErrors)
	}
}

func TestRecordAgentMetrics(t *testing.T) {
	telem := NewCallTelemetry("test-call-005")
	telem.RecordAgentWrite()
	telem.RecordAgentWrite()
	telem.RecordAgentError()

	if telem.AgentWrites != 2 {
		t.Errorf("expected AgentWrites=2, got %d", telem.AgentWrites)
	}
	if telem.AgentErrors != 1 {
		t.Errorf("expected AgentErrors=1, got %d", telem.AgentErrors)
	}
}

func TestFinalize(t *testing.T) {
	telem := NewCallTelemetry("test-call-006")
	telem.RecordRelaySetup()
	time.Sleep(10 * time.Millisecond)
	telem.RecordFrame(make([]float32, 960))
	time.Sleep(10 * time.Millisecond)
	telem.RecordFrame(make([]float32, 960))
	telem.Finalize()

	if telem.FinalizedAt.IsZero() {
		t.Error("expected FinalizedAt to be set")
	}
	if telem.DurationMs <= 0 {
		t.Errorf("expected DurationMs > 0, got %d", telem.DurationMs)
	}
	if telem.RelaySetupMs < 0 {
		t.Errorf("expected RelaySetupMs >= 0, got %d", telem.RelaySetupMs)
	}
	if telem.FirstFrameMs < 0 {
		t.Errorf("expected FirstFrameMs >= 0, got %d", telem.FirstFrameMs)
	}
	if telem.StreamMs < 0 {
		t.Errorf("expected StreamMs >= 0, got %d", telem.StreamMs)
	}

	// Finalize should be idempotent.
	original := telem.FinalizedAt
	telem.Finalize()
	if !telem.FinalizedAt.Equal(original) {
		t.Error("expected Finalize to be idempotent")
	}
}

func TestSummary(t *testing.T) {
	telem := NewCallTelemetry("test-call-007")
	telem.RecordRelaySetup()
	telem.RecordFrame(make([]float32, 960))
	telem.RecordFrame(make([]float32, 960))
	telem.RecordAgentWrite()
	telem.Finalize()

	summary := telem.Summary()
	if !strings.Contains(summary, "call=test-call-007") {
		t.Errorf("summary missing call_id: %s", summary)
	}
	if !strings.Contains(summary, "frames=2") {
		t.Errorf("summary missing frame count: %s", summary)
	}
	if !strings.Contains(summary, "sink_writes=2") {
		t.Errorf("summary missing sink_writes: %s", summary)
	}
	if !strings.Contains(summary, "agent_writes=1") {
		t.Errorf("summary missing agent_writes: %s", summary)
	}
}

// mockSink implements WriteFrame for testing TelemetrySink.
type mockSink struct {
	frames [][]float32
	err    error
}

func (m *mockSink) WriteFrame(frame []float32) error {
	if m.err != nil {
		return m.err
	}
	m.frames = append(m.frames, frame)
	return nil
}

func (m *mockSink) Close() error { return nil }

func TestTelemetrySinkRecordsFrames(t *testing.T) {
	telem := NewCallTelemetry("test-call-008")
	mock := &mockSink{}
	sink := NewTelemetrySink(mock, telem, false)

	frame := make([]float32, 960)
	if err := sink.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if err := sink.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}

	if telem.FrameCount != 2 {
		t.Errorf("expected FrameCount=2, got %d", telem.FrameCount)
	}
	if len(mock.frames) != 2 {
		t.Errorf("expected 2 frames forwarded, got %d", len(mock.frames))
	}
}

func TestTelemetrySinkRecordsErrors(t *testing.T) {
	telem := NewCallTelemetry("test-call-009")
	mock := &mockSink{err: errors.New("write failed")}
	sink := NewTelemetrySink(mock, telem, false)

	err := sink.WriteFrame(make([]float32, 960))
	if err == nil {
		t.Fatal("expected error from WriteFrame")
	}
	if telem.SinkErrors != 1 {
		t.Errorf("expected SinkErrors=1, got %d", telem.SinkErrors)
	}
	if telem.FrameCount != 0 {
		t.Errorf("expected FrameCount=0 on error, got %d", telem.FrameCount)
	}
}

func TestTelemetrySinkAgentMode(t *testing.T) {
	telem := NewCallTelemetry("test-call-010")
	mock := &mockSink{}
	sink := NewTelemetrySink(mock, telem, true)

	sink.WriteFrame(make([]float32, 960))
	sink.WriteFrame(make([]float32, 960))

	if telem.AgentWrites != 2 {
		t.Errorf("expected AgentWrites=2, got %d", telem.AgentWrites)
	}
	if telem.FrameCount != 0 {
		t.Errorf("expected FrameCount=0 in agent mode, got %d", telem.FrameCount)
	}
}

// TestTelemetryLifecycle simulates a full call lifecycle.
func TestTelemetryLifecycle(t *testing.T) {
	telem := NewCallTelemetry("lifecycle-call-001")

	// Phase 1: Setup
	telem.RecordRelaySetup()
	time.Sleep(5 * time.Millisecond)

	// Phase 2: Inbound frames (simulating caller audio)
	mock := &mockSink{}
	sink := NewTelemetrySink(mock, telem, false)
	for i := 0; i < 100; i++ {
		frame := make([]float32, 960)
		frame[0] = float32(i) / 100.0
		if err := sink.WriteFrame(frame); err != nil {
			t.Fatalf("WriteFrame %d: %v", i, err)
		}
		if i%25 == 0 {
			time.Sleep(2 * time.Millisecond) // ensure measurable time passes
		}
	}
	time.Sleep(10 * time.Millisecond)

	// Phase 3: Agent outbound frames (simulating bot audio)
	agentMock := &mockSink{}
	agentSink := NewTelemetrySink(agentMock, telem, true)
	for i := 0; i < 50; i++ {
		agentSink.WriteFrame(make([]float32, 960))
	}

	// Phase 4: Finalize
	telem.Finalize()

	// Verify
	if telem.FrameCount != 100 {
		t.Errorf("expected FrameCount=100, got %d", telem.FrameCount)
	}
	if telem.AgentWrites != 50 {
		t.Errorf("expected AgentWrites=50, got %d", telem.AgentWrites)
	}
	if telem.SinkWrites != 100 {
		t.Errorf("expected SinkWrites=100, got %d", telem.SinkWrites)
	}
	if telem.DurationMs <= 0 {
		t.Errorf("expected DurationMs > 0, got %d", telem.DurationMs)
	}
	if telem.FirstFrameMs < 0 {
		t.Errorf("expected FirstFrameMs >= 0, got %d", telem.FirstFrameMs)
	}
	if telem.StreamMs <= 0 {
		t.Errorf("expected StreamMs > 0, got %d", telem.StreamMs)
	}

	// Verify forwarded frames.
	if len(mock.frames) != 100 {
		t.Errorf("expected 100 forwarded frames, got %d", len(mock.frames))
	}
	if len(agentMock.frames) != 50 {
		t.Errorf("expected 50 agent frames, got %d", len(agentMock.frames))
	}

	// Summary should be readable.
	summary := telem.Summary()
	if !strings.Contains(summary, "frames=100") {
		t.Errorf("summary missing frames: %s", summary)
	}
}
