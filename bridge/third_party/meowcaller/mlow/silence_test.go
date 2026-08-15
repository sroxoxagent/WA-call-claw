package mlow

import (
	"math"
	"testing"
)

func TestInactiveFrameDecodesToCleanSilence(t *testing.T) {
	payload := InactiveFrame()
	if len(payload) != 1 || payload[0] != 0x10 {
		t.Fatalf("unexpected inactive payload: %x", payload)
	}
	decoded := NewMlowDecoder().Decode(payload)
	if len(decoded) != opusFrameSamps {
		t.Fatalf("decoded samples=%d, want %d", len(decoded), opusFrameSamps)
	}
	var sum float64
	for _, sample := range decoded {
		sum += float64(sample) * float64(sample)
	}
	if rms := math.Sqrt(sum / float64(len(decoded))); rms != 0 {
		t.Fatalf("decoded RMS=%g, want exact zero", rms)
	}
}
