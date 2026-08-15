package bridge

import (
	"encoding/binary"
	"io"
	"sync"
	"sync/atomic"

	"github.com/purpshell/meowcaller"
)

// TeeSink is an AudioSink that writes each frame to multiple downstream sinks.
// It implements meowcaller.AudioSink and is used to simultaneously record
// incoming audio to WAV and forward it to an agent WebSocket connection.
type TeeSink struct {
	mu    sync.Mutex
	sinks []meowcaller.AudioSink
}

// NewTeeSink creates a TeeSink that fans out to the given sinks.
func NewTeeSink(sinks ...meowcaller.AudioSink) *TeeSink {
	return &TeeSink{sinks: sinks}
}

// WriteFrame writes the frame to all attached sinks. Errors from any sink
// are returned (first error wins); partial writes may have occurred.
func (t *TeeSink) WriteFrame(frame []float32) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, s := range t.sinks {
		if err := s.WriteFrame(frame); err != nil {
			return err
		}
	}
	return nil
}

// Close closes all attached sinks.
func (t *TeeSink) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	var firstErr error
	for _, s := range t.sinks {
		if err := s.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// AgentSink is an AudioSink that converts float32 PCM frames to s16le bytes
// and sends them as binary WebSocket messages to an agent connection.
type AgentSink struct {
	conn           *AgentConn
	callID         string
	firstFrameMu   sync.Mutex
	firstFrameSeen bool
}

// NewAgentSink creates a sink that forwards frames to the agent for one call.
func NewAgentSink(conn *AgentConn, callID string) *AgentSink {
	return &AgentSink{conn: conn, callID: callID}
}

// WriteFrame converts the float32 frame to s16le and sends it as a binary
// WebSocket message. Returns an error if the agent is disconnected.
func (a *AgentSink) WriteFrame(frame []float32) error {
	buf := make([]byte, len(frame)*2)
	for i, s := range frame {
		v := s * 32768.0
		if v > 32767 {
			v = 32767
		} else if v < -32768 {
			v = -32768
		}
		binary.LittleEndian.PutUint16(buf[2*i:], uint16(int16(v)))
	}

	a.firstFrameMu.Lock()
	if !a.firstFrameSeen {
		a.firstFrameSeen = true
	}
	a.firstFrameMu.Unlock()

	return a.conn.SendBinary(PackAudioFrame(a.callID, buf))
}

// Close is a no-op; the agent connection lifecycle is managed by the server.
func (a *AgentSink) Close() error { return nil }

// AgentSource is an AudioSource that reads s16le binary frames from an agent
// WebSocket connection and converts them to float32 PCM for playback into the call.
type AgentSource struct {
	conn         *AgentConn
	expectedCall string
	done         chan struct{}
	err          error
	pending      []byte // outbound PCM not yet consumed as a 60 ms frame

	// flushing is set by StopPlayback (barge-in). While flushing, every frame
	// still queued in the agent connection's binary channel is discarded, so
	// audio the agent already sent before the stop request is never played.
	// It stays set until the next audio_playing event (new TTS turn) clears
	// it, which guarantees buffered stale audio can not leak into the call.
	flushing atomic.Bool
}

// NewAgentSource creates a source that reads audio only for one call.
func NewAgentSource(conn *AgentConn, callID string) *AgentSource {
	return &AgentSource{
		conn:         conn,
		expectedCall: callID,
		done:         make(chan struct{}),
	}
}

// StopPlayback discards all queued bot audio immediately. Every subsequent
// ReadFrame returns nil (inactive media) and drains any frames still queued
// in the connection buffer, until ResumePlayback is called (new TTS turn).
func (a *AgentSource) StopPlayback() {
	a.flushing.Store(true)
	a.pending = a.pending[:0]
}

// ResumePlayback clears the flush state so new audio frames are played again.
func (a *AgentSource) ResumePlayback() {
	a.flushing.Store(false)
}

// ReadFrame reads the next binary WebSocket message from the agent, converts
// s16le bytes to float32, and returns a frame. It must not block while the
// agent is waiting for VAD or generating a response: the meowcaller media loop
// needs a frame every 60 ms to keep the outbound relay alive. When no agent
// audio is queued, it returns a nil frame so the media loop emits the codec's
// inactive MLow payload. This is intentionally different from a PCM-zero frame:
// PCM zero would be encoded as active audio. Returns io.EOF when the agent
// disconnects.
func (a *AgentSource) ReadFrame() ([]float32, error) {
	select {
	case <-a.conn.Done():
		a.err = a.conn.closeErr
		a.markDone()
		return nil, io.EOF
	default:
	}

	const frameBytes = meowcaller.FrameSamples * 2

	// Barge-in flush: discard every frame that is still queued (including
	// frames already buffered in the agent connection's binary channel).
	// The agent sends all TTS audio in one fast burst, so by the time
	// audio_stop arrives most of the audio is already sitting in the
	// connection buffer — without this drain it would keep playing.
	for a.flushing.Load() {
		select {
		case <-a.conn.binaryCh:
			// drop stale audio frame
		default:
			return nil, nil
		}
	}

	// Preserve any remainder from a larger WebSocket message. TTS commonly
	// emits 100 ms chunks (3200 bytes), while MEOWcaller pulls 60 ms frames
	// (1920 bytes); dropping the remainder creates gaps and can sound broken.
	for len(a.pending) < frameBytes {
		select {
		case msg, ok := <-a.conn.binaryCh:
			if !ok {
				if len(a.pending) == 0 {
					a.err = a.conn.closeErr
					a.markDone()
					return nil, io.EOF
				}
				break
			}
			frameCallID, pcm, err := UnpackAudioFrame(msg)
			if err != nil || frameCallID != a.expectedCall {
				// Invalid or stale audio must never be played into this call.
				continue
			}
			a.pending = append(a.pending, pcm...)
		default:
			break
		}
		if len(a.pending) >= frameBytes {
			break
		}
		// No bot audio yet: return a nil frame immediately so the call keeps
		// sending codec-native inactive media while STT/LLM/TTS is processing.
		if len(a.pending) == 0 {
			return nil, nil
		}
		break
	}

	if len(a.pending) < frameBytes {
		// A partial chunk cannot form a complete call frame yet. Keep it queued
		// and send inactive media for this tick; the next tick will complete it.
		return nil, nil
	}

	msg := a.pending[:frameBytes]
	a.pending = a.pending[frameBytes:]
	frame := make([]float32, meowcaller.FrameSamples)
	for i := 0; i < meowcaller.FrameSamples; i++ {
		v := int16(binary.LittleEndian.Uint16(msg[2*i:]))
		frame[i] = float32(v) / 32768.0
	}
	return frame, nil
}

func (a *AgentSource) markDone() {
	select {
	case <-a.done:
	default:
		close(a.done)
	}
}

// Close signals the source is done. Safe to call multiple times.
func (a *AgentSource) Close() error {
	select {
	case <-a.done:
	default:
		close(a.done)
	}
	return nil
}

// LastError returns the error that caused ReadFrame to fail, if any.
func (a *AgentSource) LastError() error {
	return a.err
}
