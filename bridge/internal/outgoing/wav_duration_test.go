package outgoing

import (
	"os"
	"path/filepath"
	"testing"
)

func writeWav(t *testing.T, sampleRate int, channels int, dataBytes int) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "test.wav")
	f, _ := os.Create(p)
	hdr := make([]byte, 44)
	copy(hdr[0:4], "RIFF")
	// RIFF size = 36 + data
	hdr[4] = byte((36 + dataBytes) & 0xff)
	hdr[5] = byte((36 + dataBytes) >> 8 & 0xff)
	hdr[6] = byte((36 + dataBytes) >> 16 & 0xff)
	hdr[7] = byte((36 + dataBytes) >> 24 & 0xff)
	copy(hdr[8:12], "WAVE")
	copy(hdr[12:16], "fmt ")
	hdr[16] = 16
	hdr[20] = 1         // PCM
	hdr[22] = byte(channels)
	hdr[24] = byte(sampleRate & 0xff)
	hdr[25] = byte(sampleRate >> 8 & 0xff)
	hdr[26] = byte(sampleRate >> 16 & 0xff)
	hdr[27] = byte(sampleRate >> 24 & 0xff)
	byteRate := sampleRate * channels * 2
	hdr[28] = byte(byteRate & 0xff)
	hdr[29] = byte(byteRate >> 8 & 0xff)
	hdr[30] = byte(byteRate >> 16 & 0xff)
	hdr[31] = byte(byteRate >> 24 & 0xff)
	hdr[32] = byte(channels * 2)
	hdr[34] = 16 // bits per sample (offset 34-35)
	copy(hdr[36:40], "data")
	hdr[40] = byte(dataBytes & 0xff)
	hdr[41] = byte(dataBytes >> 8 & 0xff)
	hdr[42] = byte(dataBytes >> 16 & 0xff)
	hdr[43] = byte(dataBytes >> 24 & 0xff)
	f.Write(hdr)
	f.Write(make([]byte, dataBytes))
	f.Close()
	return p
}

func TestWavDurationSeconds(t *testing.T) {
	// 48kHz mono 16-bit, 280944 bytes data = 2.9265s (real panggilan.wav shape)
	p := writeWav(t, 48000, 1, 280944)
	d, err := wavDurationSeconds(p)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if d < 2.9 || d > 2.95 {
		t.Fatalf("duration = %.3f, want ~2.93", d)
	}
	// 16kHz mono, 32000 bytes = 1.0s
	p2 := writeWav(t, 16000, 1, 32000)
	d2, _ := wavDurationSeconds(p2)
	if d2 < 0.99 || d2 > 1.01 {
		t.Fatalf("duration2 = %.3f, want ~1.0", d2)
	}
	// missing file
	if _, err := wavDurationSeconds(filepath.Join(t.TempDir(), "nope.wav")); err == nil {
		t.Fatal("expected error for missing file")
	}
	// not a wav
	bad := filepath.Join(t.TempDir(), "bad.wav")
	os.WriteFile(bad, []byte("not a wav file at all"), 0o644)
	if _, err := wavDurationSeconds(bad); err == nil {
		t.Fatal("expected error for non-wav")
	}
}

func TestWavDurationRealFile(t *testing.T) {
	// Real announcement file used in production
	d, err := wavDurationSeconds("/opt/wa-call-claw/runtime/audio/panggilan.wav")
	if err != nil {
		t.Skipf("real file not present: %v", err)
	}
	if d < 2.5 || d > 3.5 {
		t.Fatalf("panggilan.wav duration = %.2f, want ~2.93", d)
	}
}
