package recorder

import (
	"io"
	"testing"

	"github.com/purpshell/meowcaller"
)

func TestSilenceFrames(t *testing.T) {
	tests := []struct {
		name     string
		ms       int
		expected int
	}{
		{"zero", 0, 0},
		{"negative", -100, 0},
		{"60ms", 60, 1},
		{"120ms", 120, 2},
		{"2000ms", 2000, 34}, // 2000/60 = 33.33 → 34
		{"2001ms", 2001, 34}, // 2001/60 = 33.35 → 34
		{"3000ms", 3000, 50}, // 3000/60 = 50
		{"100ms", 100, 2},    // 100/60 = 1.67 → 2
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := SilenceFrames(tt.ms)
			if got != tt.expected {
				t.Errorf("SilenceFrames(%d) = %d, want %d", tt.ms, got, tt.expected)
			}
		})
	}
}

func TestSilenceThenSourceSilencePhase(t *testing.T) {
	// Inner source that yields 5 frames then EOF.
	inner := &countingSource{remaining: 5}
	src := NewSilenceThenSource(inner, 2000) // 34 silence frames

	// First 34 frames should be silence (all zeros).
	for i := 0; i < 34; i++ {
		frame, err := src.ReadFrame()
		if err != nil {
			t.Fatalf("frame %d: unexpected error: %v", i, err)
		}
		if len(frame) != meowcaller.FrameSamples {
			t.Fatalf("frame %d: expected %d samples, got %d", i, meowcaller.FrameSamples, len(frame))
		}
		for j, s := range frame {
			if s != 0 {
				t.Fatalf("frame %d sample %d: expected 0 (silence), got %f", i, j, s)
			}
		}
	}

	// SilenceLeft should be 0 now.
	if src.SilenceLeft() != 0 {
		t.Errorf("SilenceLeft() = %d, want 0", src.SilenceLeft())
	}

	// Next frames should come from inner source.
	for i := 0; i < 5; i++ {
		frame, err := src.ReadFrame()
		if err != nil {
			t.Fatalf("inner frame %d: unexpected error: %v", i, err)
		}
		if frame[0] != 1.0 {
			t.Errorf("inner frame %d: expected first sample 1.0, got %f", i, frame[0])
		}
	}

	// Now EOF.
	_, err := src.ReadFrame()
	if err != io.EOF {
		t.Errorf("expected io.EOF, got %v", err)
	}
}

func TestSilenceThenSourceZeroDelay(t *testing.T) {
	inner := &countingSource{remaining: 2}
	src := NewSilenceThenSource(inner, 0) // no delay

	// Should go straight to inner source.
	frame, err := src.ReadFrame()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if frame[0] != 1.0 {
		t.Errorf("expected inner frame (1.0), got %f", frame[0])
	}
}

func TestSilenceThenSourceClose(t *testing.T) {
	inner := &countingSource{remaining: 10}
	src := NewSilenceThenSource(inner, 2000)

	if err := src.Close(); err != nil {
		t.Errorf("Close() failed: %v", err)
	}
	if !inner.closed {
		t.Error("inner source was not closed")
	}
}

// countingSource is a test AudioSource that yields `remaining` frames
// with value 1.0, then returns io.EOF.
type countingSource struct {
	remaining int
	closed    bool
}

func (c *countingSource) ReadFrame() ([]float32, error) {
	if c.remaining <= 0 {
		return nil, io.EOF
	}
	c.remaining--
	frame := make([]float32, meowcaller.FrameSamples)
	frame[0] = 1.0 // marker to identify inner frames
	return frame, nil
}

func (c *countingSource) Close() error {
	c.closed = true
	return nil
}
