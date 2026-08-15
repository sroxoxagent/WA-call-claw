package bridge

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"
)

// ControlMessage is a non-audio playback/control event from the agent.
type ControlMessage struct {
	Type   string
	CallID string
}

// AgentConn represents a single WebSocket connection from an external agent.
// It runs a single reader goroutine that dispatches binary frames to a channel
// (consumed by AgentSource) and JSON messages to handlers.
type AgentConn struct {
	conn   *websocket.Conn
	mu     sync.Mutex // protects writes
	ctx    context.Context
	cancel context.CancelFunc
	addr   string // remote address string

	// Binary frames read from the WebSocket are pushed here for AgentSource.
	binaryCh chan []byte
	// Control events are consumed by the call playback gate.
	controlCh chan ControlMessage
	// closed is closed when the reader goroutine exits.
	closed chan struct{}
	// closeErr stores the error that ended the reader.
	closeErr error

	// lastRecvUnixNano is the unix-nano timestamp of the last message
	// received from the agent (any type). Updated by readPump; read by the
	// ping watchdog to prove liveness via traffic.
	lastRecvUnixNano atomic.Int64

	// dropped counts binary frames dropped when the channel was full.
	dropped atomic.Int64
}

// NewAgentConn wraps a raw websocket.Conn into an AgentConn and starts
// the reader goroutine. addr is the remote address string for logging.
// Call Close() when done.
func NewAgentConn(ctx context.Context, conn *websocket.Conn, addr string) *AgentConn {
	ctx, cancel := context.WithCancel(ctx)
	a := &AgentConn{
		conn:      conn,
		ctx:       ctx,
		cancel:    cancel,
		addr:      addr,
		binaryCh:  make(chan []byte, 64), // small buffer for jitter
		controlCh: make(chan ControlMessage, 16),
		closed:    make(chan struct{}),
	}
	go a.readPump()
	return a
}

// readPump is the single reader goroutine. It reads all messages from the
// WebSocket and dispatches: binary → binaryCh, JSON → handlers.
func (a *AgentConn) readPump() {
	defer close(a.closed)
	defer close(a.controlCh)
	defer a.cancel()

	for {
		typ, data, err := a.conn.Read(a.ctx)
		if err != nil {
			a.closeErr = err
			log.Printf("[bridge] agent read loop ended: %v", err)
			// Unblock any pending ReadFrame by closing the channel.
			close(a.binaryCh)
			return
		}
		// Any message from the agent proves the connection is alive.
		a.lastRecvUnixNano.Store(time.Now().UnixNano())
		switch typ {
		case websocket.MessageBinary:
			// Copy data since the WebSocket library may reuse the buffer.
			frame := make([]byte, len(data))
			copy(frame, data)
			select {
			case a.binaryCh <- frame:
			case <-a.ctx.Done():
				return
			default:
				// Channel full — drop the frame instead of blocking the
				// reader. Blocking here stalls ping/pong handling: the
				// agent's keepalive ping goes unanswered and the bridge
				// watchdog kills the connection ~20s after a call ends
				// (the call media loop stops draining the channel, so a
				// burst of TTS audio leaves readPump stuck forever).
				// Dropped frames only occur when the agent bursts audio
				// faster than the media loop can play it (barge-in /
				// call teardown) — acceptable loss, keeps the link alive.
				a.dropped.Add(1)
				if n := a.dropped.Load(); n == 1 || n%100 == 0 {
					log.Printf("[bridge] dropped %d agent audio frame(s) (binary channel full)", n)
				}
			}
		case websocket.MessageText:
			a.handleText(data)
		}
	}
}

// handleText processes JSON text messages from the agent.
func (a *AgentConn) handleText(data []byte) {
	var msg struct {
		Type   string `json:"type"`
		CallID string `json:"call_id"`
		Reason string `json:"reason"`
	}
	if err := json.Unmarshal(data, &msg); err != nil {
		log.Printf("[bridge] invalid JSON from agent: %v", err)
		return
	}
	switch msg.Type {
	case "hangup":
		log.Printf("[bridge] agent requested hangup: reason=%s", msg.Reason)
		// The hangup is handled by the caller that owns the AgentConn.
		// We signal it by cancelling the context.
		a.cancel()
	case "audio_playing", "audio_done", "audio_stop":
		select {
		case a.controlCh <- ControlMessage{Type: msg.Type, CallID: msg.CallID}:
		default:
			log.Printf("[bridge] dropped playback control event: type=%s call_id=%s", msg.Type, msg.CallID)
		}
	default:
		log.Printf("[bridge] unknown JSON type from agent: %s", msg.Type)
	}
}

// SendJSON marshals v to JSON and sends it as a text WebSocket message.
func (a *AgentConn) SendJSON(v interface{}) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	data, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal JSON: %w", err)
	}
	return a.conn.Write(a.ctx, websocket.MessageText, data)
}

// SendBinary sends raw bytes as a binary WebSocket message.
func (a *AgentConn) SendBinary(data []byte) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.conn.Write(a.ctx, websocket.MessageBinary, data)
}

// Controls returns playback/control events from the agent.
func (a *AgentConn) Controls() <-chan ControlMessage {
	return a.controlCh
}

// DrainBinary discards binary audio that was queued before a call ended.
// The WebSocket connection is intentionally persistent across calls, so the
// channel must be reset at each call boundary to prevent stale PCM from being
// consumed by the next call's AgentSource.
func (a *AgentConn) DrainBinary() (drained int) {
	for {
		select {
		case _, ok := <-a.binaryCh:
			if !ok {
				return drained
			}
			drained++
		default:
			return drained
		}
	}
}

// ReadBinary returns the next binary frame from the agent, or an error
// if the connection is closed. Used by AgentSource.
func (a *AgentConn) ReadBinary() ([]byte, error) {
	select {
	case frame, ok := <-a.binaryCh:
		if !ok {
			return nil, a.closeErr
		}
		return frame, nil
	case <-a.ctx.Done():
		return nil, a.ctx.Err()
	}
}

// Close closes the WebSocket connection and cancels the context.
func (a *AgentConn) Close() error {
	a.cancel()
	return a.conn.Close(websocket.StatusNormalClosure, "bridge closed")
}

// RemoteAddr returns the remote network address string.
func (a *AgentConn) RemoteAddr() string {
	return a.addr
}

// Done returns a channel that's closed when the reader goroutine exits.
func (a *AgentConn) Done() <-chan struct{} {
	return a.closed
}

// HangupRequested returns true if the agent sent a hangup message.
func (a *AgentConn) HangupRequested() bool {
	select {
	case <-a.ctx.Done():
		return true
	default:
		return false
	}
}

// LastRecv returns the time the last message was received from the agent
// (zero value if nothing has been received yet).
func (a *AgentConn) LastRecv() time.Time {
	return time.Unix(0, a.lastRecvUnixNano.Load())
}
