package recorder

import (
	"io"
	"os"
	"testing"

	"github.com/purpshell/meowcaller"
)

// TestWAVFileSource verifies that meowcaller.WAVFile compiles and can read
// the test WAV file, producing valid audio frames. This validates the playback
// API is compile- and runtime-ready.
func TestWAVFileSource(t *testing.T) {
	wavPath := "../../runtime/playback.wav"
	if _, err := os.Stat(wavPath); os.IsNotExist(err) {
		t.Skip("playback.wav not found, skipping WAV source test")
	}

	src, err := meowcaller.WAVFile(wavPath)
	if err != nil {
		t.Fatalf("WAVFile(%s) failed: %v", wavPath, err)
	}
	defer src.Close()

	// Read frames until EOF to validate the full decode pipeline.
	var frameCount int
	for {
		frame, err := src.ReadFrame()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("ReadFrame failed at frame %d: %v", frameCount, err)
		}
		if len(frame) != meowcaller.FrameSamples {
			t.Fatalf("frame %d: expected %d samples, got %d", frameCount, meowcaller.FrameSamples, len(frame))
		}
		frameCount++
	}

	if frameCount == 0 {
		t.Fatal("WAVFile produced 0 frames")
	}
	t.Logf("WAVFile: decoded %d frames (%d samples total, ~%.1fs at %dHz)",
		frameCount, frameCount*meowcaller.FrameSamples,
		float64(frameCount*meowcaller.FrameSamples)/float64(meowcaller.SampleRate),
		meowcaller.SampleRate)
}

// TestWAVFileInstrumentedSink verifies the full pipeline: WAVFile → InstrumentedSink.
func TestWAVFileInstrumentedSink(t *testing.T) {
	wavPath := "../../runtime/playback.wav"
	if _, err := os.Stat(wavPath); os.IsNotExist(err) {
		t.Skip("playback.wav not found, skipping WAV+sink test")
	}

	src, err := meowcaller.WAVFile(wavPath)
	if err != nil {
		t.Fatalf("WAVFile failed: %v", err)
	}
	defer src.Close()

	// Create a WAV recorder with instrumented sink.
	dir := t.TempDir()
	recPath := dir + "/test.wav-playback.wav"
	rec, err := NewWAVRecorder(recPath)
	if err != nil {
		t.Fatalf("NewWAVRecorder failed: %v", err)
	}

	sink := rec.InstrumentedSink()

	// Pipe WAV frames into the instrumented sink.
	var frameCount int
	for {
		frame, err := src.ReadFrame()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("ReadFrame failed: %v", err)
		}
		if err := sink.WriteFrame(frame); err != nil {
			t.Fatalf("WriteFrame failed: %v", err)
		}
		frameCount++
	}

	if err := rec.Finish(); err != nil {
		t.Fatalf("Finish failed: %v", err)
	}

	// Verify instrumentation.
	if rec.FrameCount() != uint64(frameCount) {
		t.Errorf("frame count mismatch: recorder=%d, actual=%d", rec.FrameCount(), frameCount)
	}
	if rec.LastRMS() <= 0 {
		t.Error("expected positive RMS for non-silent WAV")
	}

	t.Logf("WAV→WAV pipeline: %d frames, RMS=%.6f, duration=%dms",
		rec.FrameCount(), rec.LastRMS(), rec.DurationMs())
}

// TestWAVFileWithSilenceThenSource verifies the full delayed-playback pipeline:
// WAVFile → SilenceThenSource(2s) → frames. This validates the exact path
// used in production (answer → 2s delay → play announcement).
func TestWAVFileWithSilenceThenSource(t *testing.T) {
	wavPath := "../../runtime/playback.wav"
	if _, err := os.Stat(wavPath); os.IsNotExist(err) {
		t.Skip("playback.wav not found, skipping delay pipeline test")
	}

	src, err := meowcaller.WAVFile(wavPath)
	if err != nil {
		t.Fatalf("WAVFile failed: %v", err)
	}
	defer src.Close()

	// Wrap with 2-second silence delay (same as production).
	wrapped := NewSilenceThenSource(src, SilenceDurationMs)

	// First 34 frames should be silence (2000ms / 60ms per frame = 33.33 → 34).
	silenceFrames := SilenceFrames(SilenceDurationMs)
	for i := 0; i < silenceFrames; i++ {
		frame, err := wrapped.ReadFrame()
		if err != nil {
			t.Fatalf("silence frame %d: unexpected error: %v", i, err)
		}
		for j, s := range frame {
			if s != 0 {
				t.Fatalf("silence frame %d sample %d: expected 0, got %f", i, j, s)
			}
		}
	}

	// After silence, verify we get actual audio frames from WAV source.
	audioFrame, err := wrapped.ReadFrame()
	if err != nil {
		t.Fatalf("first audio frame after silence: %v", err)
	}
	// WAV source should produce non-silence frames.
	allSilent := true
	for _, s := range audioFrame {
		if s != 0 {
			allSilent = false
			break
		}
	}
	if allSilent {
		t.Error("expected non-silent audio frame after silence delay, got all zeros")
	}

	t.Logf("delay pipeline OK: %d silence frames then audio flowing", silenceFrames)
}
