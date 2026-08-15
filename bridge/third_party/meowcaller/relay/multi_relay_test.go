package relay

import (
	"encoding/binary"
	"testing"
	"time"

	"github.com/rs/zerolog"
)

func TestDedupKey(t *testing.T) {
	// Same SSRC+seq should produce same key.
	k1 := dedupKey(0x12345678, 42)
	k2 := dedupKey(0x12345678, 42)
	if k1 != k2 {
		t.Errorf("dedupKey returned different keys for same inputs: %d != %d", k1, k2)
	}
	// Different SSRC should produce different key.
	k3 := dedupKey(0x12345679, 42)
	if k1 == k3 {
		t.Errorf("dedupKey returned same key for different SSRC")
	}
	// Different seq should produce different key.
	k4 := dedupKey(0x12345678, 43)
	if k1 == k4 {
		t.Errorf("dedupKey returned same key for different seq")
	}
}

func TestMultiRelayChannelIsDuplicate(t *testing.T) {
	m := &MultiRelayChannel{
		dedupMap: make(map[uint64]time.Time),
		dedupTTL: 30 * time.Second,
	}

	// Build a minimal RTP packet (12 bytes header).
	makeRTP := func(ssrc uint32, seq uint16) []byte {
		pkt := make([]byte, 12)
		pkt[0] = 0x80 // V=2, P=0, X=0, CC=0
		pkt[1] = 0    // M=0, PT=0
		binary.BigEndian.PutUint16(pkt[2:4], seq)
		binary.BigEndian.PutUint32(pkt[8:12], ssrc)
		return pkt
	}

	// First packet should not be duplicate.
	pkt1 := makeRTP(0x12345678, 1)
	if m.isDuplicate(pkt1) {
		t.Error("first packet should not be duplicate")
	}

	// Same packet again should be duplicate.
	if !m.isDuplicate(pkt1) {
		t.Error("same packet should be duplicate")
	}

	// Different seq should not be duplicate.
	pkt2 := makeRTP(0x12345678, 2)
	if m.isDuplicate(pkt2) {
		t.Error("different seq should not be duplicate")
	}

	// Different SSRC same seq should not be duplicate.
	pkt3 := makeRTP(0x12345679, 1)
	if m.isDuplicate(pkt3) {
		t.Error("different SSRC should not be duplicate")
	}

	// Non-RTP packet (< 12 bytes) should never be duplicate.
	shortPkt := []byte{0x00, 0x01}
	if m.isDuplicate(shortPkt) {
		t.Error("short packet should not be duplicate")
	}

	// STUN packet (first byte & 0xc0 == 0) should never be duplicate.
	stunPkt := make([]byte, 20)
	stunPkt[0] = 0x00
	if m.isDuplicate(stunPkt) {
		t.Error("STUN packet should not be duplicate")
	}
}

func TestMultiRelayChannelNumChannels(t *testing.T) {
	m := &MultiRelayChannel{
		channels: []*RelayMediaChannel{nil, nil, nil},
	}
	if m.NumChannels() != 3 {
		t.Errorf("NumChannels() = %d, want 3", m.NumChannels())
	}
}

func TestNewMultiRelayChannelNilSlice(t *testing.T) {
	m := NewMultiRelayChannel(nil, zerologNop())
	if m != nil {
		t.Error("NewMultiRelayChannel(nil) should return nil")
	}
}

// zerologNop returns a nop zerolog.Logger for tests.
func zerologNop() zerolog.Logger {
	return zerolog.Nop()
}
