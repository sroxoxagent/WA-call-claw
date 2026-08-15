package mlow

// InactiveFrame returns the canonical 60 ms / 16 kHz MLow inactive payload.
//
// This is a valid one-byte MLow TOC frame: 0x10 means 16 kHz, 60 ms,
// no VAD and inactive. Receivers route it to codec silence instead of
// decoding a PCM-zero frame as an active MLow packet with a low noise floor.
func InactiveFrame() []byte {
	return []byte{0x10}
}
