package bridge

import (
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/purpshell/meowcaller"
)

// TestDrainBinaryClearsQueuedFrames verifies call-boundary cleanup for the
// persistent agent WebSocket connection.
func TestDrainBinaryClearsQueuedFrames(t *testing.T) {
	conn := &AgentConn{binaryCh: make(chan []byte, 4)}
	conn.binaryCh <- []byte{1}
	conn.binaryCh <- []byte{2}
	if drained := conn.DrainBinary(); drained != 2 {
		t.Fatalf("expected 2 drained frames, got %d", drained)
	}
	select {
	case frame := <-conn.binaryCh:
		t.Fatalf("stale frame remained in queue: %v", frame)
	default:
	}
	if drained := conn.DrainBinary(); drained != 0 {
		t.Fatalf("expected empty queue on second drain, got %d", drained)
	}
}

// TestAudioFormat verifies the default audio format constants.
func TestAudioFormat(t *testing.T) {
	af := DefaultAudioFormat()
	if af.SampleRate != 16000 {
		t.Errorf("expected SampleRate=16000, got %d", af.SampleRate)
	}
	if af.Channels != 1 {
		t.Errorf("expected Channels=1, got %d", af.Channels)
	}
	if af.Encoding != "s16le" {
		t.Errorf("expected Encoding=s16le, got %s", af.Encoding)
	}
	if af.FrameSize != 1920 {
		t.Errorf("expected FrameSize=1920, got %d", af.FrameSize)
	}
	if af.FrameMs != 60 {
		t.Errorf("expected FrameMs=60, got %d", af.FrameMs)
	}
}

// TestCallStartMsgJSON verifies JSON marshaling/unmarshaling of CallStartMsg.
func TestCallStartMsgJSON(t *testing.T) {
	now := time.Now().Truncate(time.Millisecond)
	msg := CallStartMsg{
		Type:      "call_started",
		CallID:    "test-call-id",
		CallerID:  "120363xxx@g.us",
		StartedAt: now,
		Audio:     DefaultAudioFormat(),
	}

	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded CallStartMsg
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if decoded.Type != "call_started" {
		t.Errorf("expected type=call_started, got %s", decoded.Type)
	}
	if decoded.CallID != "test-call-id" {
		t.Errorf("expected call_id=test-call-id, got %s", decoded.CallID)
	}
	if decoded.CallerID != "120363xxx@g.us" {
		t.Errorf("expected caller_id=120363xxx@g.us, got %s", decoded.CallerID)
	}
	if !decoded.StartedAt.Equal(now) {
		t.Errorf("started_at mismatch: %v != %v", decoded.StartedAt, now)
	}
	if decoded.Audio.SampleRate != 16000 {
		t.Errorf("expected audio.sample_rate=16000, got %d", decoded.Audio.SampleRate)
	}
}

// TestHangupMsgJSON verifies JSON marshaling/unmarshaling of HangupMsg.
func TestHangupMsgJSON(t *testing.T) {
	msg := HangupMsg{Type: "hangup", Reason: "agent done"}
	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded HangupMsg
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if decoded.Type != "hangup" {
		t.Errorf("expected type=hangup, got %s", decoded.Type)
	}
	if decoded.Reason != "agent done" {
		t.Errorf("expected reason=agent done, got %s", decoded.Reason)
	}
}

// TestTeeSinkWritesToAll verifies TeeSink fans out to multiple sinks.
func TestTeeSinkWritesToAll(t *testing.T) {
	var frames1, frames2 [][]float32
	sink1 := meowcaller.SinkFunc(func(frame []float32) {
		frames1 = append(frames1, frame)
	})
	sink2 := meowcaller.SinkFunc(func(frame []float32) {
		frames2 = append(frames2, frame)
	})

	tee := NewTeeSink(sink1, sink2)

	frame := make([]float32, meowcaller.FrameSamples)
	frame[0] = 0.5
	if err := tee.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}

	if len(frames1) != 1 {
		t.Errorf("expected 1 frame in sink1, got %d", len(frames1))
	}
	if len(frames2) != 1 {
		t.Errorf("expected 1 frame in sink2, got %d", len(frames2))
	}
	if frames1[0][0] != 0.5 {
		t.Errorf("expected frame[0]=0.5 in sink1, got %f", frames1[0][0])
	}
	if frames2[0][0] != 0.5 {
		t.Errorf("expected frame[0]=0.5 in sink2, got %f", frames2[0][0])
	}
}

// TestTeeSinkCloseAll verifies TeeSink.Close doesn't panic on empty sink.
func TestTeeSinkCloseAll(t *testing.T) {
	tee := NewTeeSink()
	if err := tee.Close(); err != nil {
		t.Errorf("Close on empty tee: %v", err)
	}
}

// TestAgentSinkConversion verifies s16le conversion from float32 frames.
func TestAgentSinkConversion(t *testing.T) {
	// Set up a WebSocket server and client for testing.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	serverConnCh := make(chan *websocket.Conn, 1)
	go func() {
		srv := &http.Server{
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
					OriginPatterns: []string{"*"},
				})
				if err != nil {
					return
				}
				serverConnCh <- conn
			}),
		}
		srv.Serve(ln)
	}()

	// Connect client.
	clientConn, _, err := websocket.Dial(t.Context(), "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer clientConn.Close(websocket.StatusNormalClosure, "done")

	// Get server-side connection.
	serverConn := <-serverConnCh
	defer serverConn.Close(websocket.StatusNormalClosure, "done")

	agentConn := NewAgentConn(t.Context(), serverConn, "test")
	sink := NewAgentSink(agentConn, "test")

	// Write a frame with known values.
	frame := make([]float32, meowcaller.FrameSamples)
	frame[0] = 0.5  // should map to 16384
	frame[1] = -0.5 // should map to -16384
	frame[2] = 1.0  // should map to 32767 (clamped)
	frame[3] = -1.0 // should map to -32768 (clamped)

	if err := sink.WriteFrame(frame); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}

	// Read from client side.
	_, data, err := clientConn.Read(t.Context())
	if err != nil {
		t.Fatalf("read: %v", err)
	}

	frameCallID, pcm, err := UnpackAudioFrame(data)
	if err != nil {
		t.Fatalf("unpack audio frame: %v", err)
	}
	if frameCallID != "test" {
		t.Fatalf("expected call_id=test, got %q", frameCallID)
	}
	if len(pcm) != meowcaller.FrameSamples*2 {
		t.Fatalf("expected %d PCM bytes, got %d", meowcaller.FrameSamples*2, len(pcm))
	}

	// Verify s16le conversion.
	v0 := int16(binary.LittleEndian.Uint16(pcm[0:]))
	if v0 != 16384 {
		t.Errorf("sample 0: expected 16384, got %d", v0)
	}
	v1 := int16(binary.LittleEndian.Uint16(pcm[2:]))
	if v1 != -16384 {
		t.Errorf("sample 1: expected -16384, got %d", v1)
	}
	v2 := int16(binary.LittleEndian.Uint16(pcm[4:]))
	if v2 != 32767 {
		t.Errorf("sample 2: expected 32767, got %d", v2)
	}
	v3 := int16(binary.LittleEndian.Uint16(pcm[6:]))
	if v3 != -32768 {
		t.Errorf("sample 3: expected -32768, got %d", v3)
	}
}

// TestAgentSourceReturnsInactiveWhenQueueEmpty verifies that playback does not
// block while STT/LLM/TTS is processing. A nil frame lets the media loop emit
// codec-native inactive MLow media until bot audio is available.
func TestAgentSourceReturnsInactiveWhenQueueEmpty(t *testing.T) {
	conn := &AgentConn{
		binaryCh: make(chan []byte),
		closed:   make(chan struct{}),
	}
	source := NewAgentSource(conn, "test")

	started := time.Now()
	frame, err := source.ReadFrame()
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	if elapsed := time.Since(started); elapsed > 100*time.Millisecond {
		t.Fatalf("ReadFrame blocked for %v while queue was empty", elapsed)
	}
	if frame != nil {
		t.Fatalf("expected nil inactive frame, got %d samples", len(frame))
	}
}

// TestAgentSourceReadsBinaryFrames verifies AgentSource reads and converts frames.
func TestAgentSourceReadsBinaryFrames(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	serverConnCh := make(chan *websocket.Conn, 1)
	go func() {
		srv := &http.Server{
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
					OriginPatterns: []string{"*"},
				})
				if err != nil {
					return
				}
				serverConnCh <- conn
			}),
		}
		srv.Serve(ln)
	}()

	// Connect client.
	clientConn, _, err := websocket.Dial(t.Context(), "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer clientConn.Close(websocket.StatusNormalClosure, "done")

	serverConn := <-serverConnCh
	defer serverConn.Close(websocket.StatusNormalClosure, "done")

	agentConn := NewAgentConn(t.Context(), serverConn, "test")
	source := NewAgentSource(agentConn, "test")

	// Send a known s16le frame from the client.
	buf := make([]byte, meowcaller.FrameSamples*2)
	binary.LittleEndian.PutUint16(buf[0:], uint16(16384)) // 0.5
	v16384 := int16(-16384)
	binary.LittleEndian.PutUint16(buf[2:], uint16(v16384)) // -0.5

	if err := clientConn.Write(t.Context(), websocket.MessageBinary, PackAudioFrame("test", buf)); err != nil {
		t.Fatalf("write: %v", err)
	}

	// Read frame from source. The production source is intentionally
	// non-blocking, so allow the reader goroutine a short window to enqueue
	// the WebSocket frame before asserting its contents.
	var frame []float32
	for attempt := 0; attempt < 100; attempt++ {
		frame, err = source.ReadFrame()
		if err != nil {
			t.Fatalf("ReadFrame: %v", err)
		}
		if len(frame) >= 2 && (frame[0] != 0 || frame[1] != 0) {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}

	if len(frame) != meowcaller.FrameSamples {
		t.Fatalf("expected %d samples, got %d", meowcaller.FrameSamples, len(frame))
	}

	if frame[0] < 0.49 || frame[0] > 0.51 {
		t.Errorf("sample 0: expected ~0.5, got %f", frame[0])
	}
	if frame[1] < -0.51 || frame[1] > -0.49 {
		t.Errorf("sample 1: expected ~-0.5, got %f", frame[1])
	}
}

// TestAgentSourceEOFOnDisconnect verifies AgentSource returns EOF when agent disconnects.
func TestAgentSourceEOFOnDisconnect(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	serverConnCh := make(chan *websocket.Conn, 1)
	go func() {
		srv := &http.Server{
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
					OriginPatterns: []string{"*"},
				})
				if err != nil {
					return
				}
				serverConnCh <- conn
			}),
		}
		srv.Serve(ln)
	}()

	clientConn, _, err := websocket.Dial(t.Context(), "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	serverConn := <-serverConnCh
	agentConn := NewAgentConn(t.Context(), serverConn, "test")
	source := NewAgentSource(agentConn, "test")

	// Disconnect client.
	clientConn.Close(websocket.StatusNormalClosure, "bye")

	// Source should return EOF.
	_, err = source.ReadFrame()
	if err != io.EOF {
		t.Errorf("expected io.EOF, got %v", err)
	}
}

// TestServerAcceptsOneAgent verifies the server rejects a second agent with 409.
func TestServerAcceptsOneAgent(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	srv := &Server{
		listen: ln.Addr().String(),
		path:   "/ws",
	}
	srv.ln = ln

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", srv.handleUpgrade)
	srv.srv = &http.Server{
		Addr:    ln.Addr().String(),
		Handler: mux,
	}

	go srv.srv.Serve(ln)

	// First agent connects.
	conn1, _, err := websocket.Dial(t.Context(), "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		t.Fatalf("dial 1: %v", err)
	}
	defer conn1.Close(websocket.StatusNormalClosure, "done")

	time.Sleep(50 * time.Millisecond) // let server register

	if !srv.HasAgent() {
		t.Fatal("expected agent to be connected after first dial")
	}

	// Second agent should get 409.
	resp, err := http.Get("http://" + ln.Addr().String() + "/ws")
	if err == nil {
		resp.Body.Close()
		if resp.StatusCode != http.StatusConflict {
			t.Errorf("expected 409, got %d", resp.StatusCode)
		}
	}
}

// TestServerAgentDisconnectCleanup verifies the server cleans up on agent disconnect.
func TestServerAgentDisconnectCleanup(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	srv := &Server{
		listen: ln.Addr().String(),
		path:   "/ws",
	}
	srv.ln = ln

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", srv.handleUpgrade)
	srv.srv = &http.Server{
		Addr:    ln.Addr().String(),
		Handler: mux,
	}

	go srv.srv.Serve(ln)
	defer srv.srv.Close()

	// Connect agent.
	conn, _, err := websocket.Dial(t.Context(), "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	time.Sleep(50 * time.Millisecond)
	if !srv.HasAgent() {
		t.Fatal("expected agent connected")
	}

	// Disconnect.
	conn.Close(websocket.StatusNormalClosure, "bye")

	// Wait for cleanup.
	time.Sleep(200 * time.Millisecond)
	if srv.HasAgent() {
		t.Error("expected agent to be nil after disconnect")
	}
}
