package recorder

import (
	"bytes"
	"math"
	"os"
	"testing"

	"github.com/purpshell/meowcaller"
)

func TestNewWAVRecorder(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	if rec.Path() != path {
		t.Errorf("expected path %s, got %s", path, rec.Path())
	}
	if rec.IsFinished() {
		t.Error("expected recorder to not be finished initially")
	}
	if rec.FrameCount() != 0 {
		t.Errorf("expected 0 frames initially, got %d", rec.FrameCount())
	}
}

func TestWAVRecorderFinish(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	// Write one valid audio frame, then finish so the WAV header is finalized.
	frame := make([]float32, meowcaller.FrameSamples)
	if err := rec.Sink().WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame failed: %v", err)
	}

	err = rec.Finish()
	if err != nil {
		t.Fatalf("Finish failed: %v", err)
	}

	if !rec.IsFinished() {
		t.Error("expected recorder to be finished after Finish()")
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read finalized WAV failed: %v", err)
	}
	if len(data) != 44+meowcaller.FrameSamples*2 {
		t.Fatalf("unexpected WAV size: got %d bytes", len(data))
	}
	if !bytes.Equal(data[0:4], []byte("RIFF")) || !bytes.Equal(data[8:12], []byte("WAVE")) {
		t.Fatalf("invalid WAV signature: %q / %q", data[0:4], data[8:12])
	}

	// Double finish should be safe.
	err = rec.Finish()
	if err != nil {
		t.Fatalf("second Finish failed: %v", err)
	}
}

func TestWAVRecorderSink(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.Sink()
	if sink == nil {
		t.Error("expected non-nil sink")
	}
}

func TestInstrumentedSinkFrameCount(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.InstrumentedSink()
	if sink == nil {
		t.Fatal("expected non-nil instrumented sink")
	}

	// Write 3 frames and verify frame count.
	for i := 0; i < 3; i++ {
		frame := make([]float32, meowcaller.FrameSamples)
		// Put a small signal in the frame so RMS is non-zero.
		frame[0] = 0.5
		if err := sink.WriteFrame(frame); err != nil {
			t.Fatalf("WriteFrame %d failed: %v", i, err)
		}
	}

	if rec.FrameCount() != 3 {
		t.Errorf("expected 3 frames, got %d", rec.FrameCount())
	}
}

func TestInstrumentedSinkRMS(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.InstrumentedSink()

	// Write a frame with known signal: all samples = 0.5
	// RMS = sqrt(mean(0.5^2)) = sqrt(0.25) = 0.5
	frame := make([]float32, meowcaller.FrameSamples)
	for i := range frame {
		frame[i] = 0.5
	}
	if err := sink.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame failed: %v", err)
	}

	rms := rec.LastRMS()
	expectedRMS := 0.5
	if math.Abs(rms-expectedRMS) > 1e-6 {
		t.Errorf("expected RMS ~%.6f, got %.6f", expectedRMS, rms)
	}
}

func TestInstrumentedSinkSilence(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.InstrumentedSink()

	// Write silence frame: all zeros → RMS = 0
	frame := make([]float32, meowcaller.FrameSamples)
	if err := sink.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame failed: %v", err)
	}

	rms := rec.LastRMS()
	if rms != 0.0 {
		t.Errorf("expected RMS 0.0 for silence, got %.6f", rms)
	}
}

func TestInstrumentedSinkFinish(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.InstrumentedSink()

	// Write some frames via instrumented sink, then finish.
	for i := 0; i < 5; i++ {
		frame := make([]float32, meowcaller.FrameSamples)
		frame[0] = float32(i) * 0.1
		if err := sink.WriteFrame(frame); err != nil {
			t.Fatalf("WriteFrame %d failed: %v", i, err)
		}
	}

	if err := rec.Finish(); err != nil {
		t.Fatalf("Finish failed: %v", err)
	}

	if !rec.IsFinished() {
		t.Error("expected finished after Finish()")
	}

	// Verify WAV file exists and has data.
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read WAV failed: %v", err)
	}
	expectedSize := 44 + 5*meowcaller.FrameSamples*2
	if len(data) != expectedSize {
		t.Fatalf("unexpected WAV size: got %d, expected %d", len(data), expectedSize)
	}
}

func TestDurationMs(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/test-call/incoming.wav"

	rec, err := NewWAVRecorder(path)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	// DurationMs should be >= 0 and small right after creation.
	d := rec.DurationMs()
	if d < 0 {
		t.Errorf("expected non-negative duration, got %d", d)
	}
}
