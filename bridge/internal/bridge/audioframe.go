package bridge

import (
	"encoding/binary"
	"fmt"
)

const (
	audioFrameVersion = 1
	audioHeaderSize   = 7 // magic(4) + version(1) + call_id_len(2)
)

var audioFrameMagic = [4]byte{'M', 'C', 'I', 'D'}

// PackAudioFrame wraps PCM with the call ID while keeping the payload binary.
func PackAudioFrame(callID string, pcm []byte) []byte {
	id := []byte(callID)
	if len(id) > 0xffff {
		panic("call ID is too long")
	}
	out := make([]byte, audioHeaderSize+len(id)+len(pcm))
	copy(out[:4], audioFrameMagic[:])
	out[4] = audioFrameVersion
	binary.BigEndian.PutUint16(out[5:7], uint16(len(id)))
	copy(out[7:7+len(id)], id)
	copy(out[7+len(id):], pcm)
	return out
}

// UnpackAudioFrame validates and removes the call-ID envelope.
func UnpackAudioFrame(data []byte) (callID string, pcm []byte, err error) {
	if len(data) < audioHeaderSize {
		return "", nil, fmt.Errorf("audio frame too short")
	}
	if string(data[:4]) != string(audioFrameMagic[:]) {
		return "", nil, fmt.Errorf("invalid audio frame magic")
	}
	if data[4] != audioFrameVersion {
		return "", nil, fmt.Errorf("unsupported audio frame version: %d", data[4])
	}
	idLen := int(binary.BigEndian.Uint16(data[5:7]))
	if len(data) < audioHeaderSize+idLen {
		return "", nil, fmt.Errorf("invalid call ID length: %d", idLen)
	}
	return string(data[7 : 7+idLen]), data[7+idLen:], nil
}
