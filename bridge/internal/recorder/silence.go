package recorder

import (
	"github.com/purpshell/meowcaller"
)

// SilenceDurationMs is the default delay (milliseconds) before playback starts.
const SilenceDurationMs = 2000

// SilenceFrames returns the number of silence frames needed for the given
// duration, based on meowcaller's 60 ms frame cadence (16 kHz × 60 ms = 960 samples).
func SilenceFrames(durationMs int) int {
	if durationMs <= 0 {
		return 0
	}
	// Each frame = 60 ms.  Round up so we get at least durationMs of silence.
	frames := durationMs / 60
	if durationMs%60 != 0 {
		frames++
	}
	return frames
}

// SilenceThenSource wraps an AudioSource and returns zero-valued (silent) frames
// for a configurable number of frames before delegating to the inner source.
// This implements a "2-second silence before playback" pattern for incoming calls.
type SilenceThenSource struct {
	inner       meowcaller.AudioSource
	silenceLeft int
	done        bool
}

// NewSilenceThenSource wraps inner with durationMs milliseconds of leading silence.
func NewSilenceThenSource(inner meowcaller.AudioSource, durationMs int) *SilenceThenSource {
	return &SilenceThenSource{
		inner:       inner,
		silenceLeft: SilenceFrames(durationMs),
	}
}

// ReadFrame returns a silence frame while the countdown is active, then
// delegates to the inner source. Returns io.EOF when both are exhausted.
func (s *SilenceThenSource) ReadFrame() ([]float32, error) {
	// Drain silence frames first.
	if s.silenceLeft > 0 {
		s.silenceLeft--
		return make([]float32, meowcaller.FrameSamples), nil
	}
	// Delegate to inner source.
	return s.inner.ReadFrame()
}

// Close closes the inner source.
func (s *SilenceThenSource) Close() error {
	if s.inner != nil {
		return s.inner.Close()
	}
	return nil
}

// SilenceLeft returns the remaining silence frames (for testing).
func (s *SilenceThenSource) SilenceLeft() int {
	return s.silenceLeft
}
