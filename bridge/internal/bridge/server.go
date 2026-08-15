package bridge

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// Server is a minimal WebSocket server that accepts exactly one agent connection.
// When an active call arrives and an agent is connected, the bridge forwards
// audio bidirectionally. Only one agent connection is allowed at a time;
// new connections while one is active are rejected with HTTP 409.
//
// The server also exposes an HTTP endpoint POST /api/call for outbound call
// requests (see SetOutgoingHandler). This is deliberately separate from the
// agent WebSocket so the single voice-agent connection is never contended.
type Server struct {
	listen string // e.g. "127.0.0.1:9090"
	path   string // URL path, e.g. "/ws"

	mu     sync.Mutex
	agent  *AgentConn
	closed bool

	// agentSrcs maps callID → *AgentSource for the single controls
	// dispatcher goroutine. Previously each call spawned its own
	// `for ctrl := range agent.Controls()` goroutine; since the controls
	// channel is per-connection (not per-call) those goroutines never
	// exited and raced for every control message, silently stealing
	// audio_playing/audio_stop of later calls (agent audio never resumed).
	agentSrcs sync.Map

	outgoingHandler OutgoingHandler

	ln  net.Listener
	srv *http.Server
}

// OutgoingHandler processes an outbound call request and returns an ack.
type OutgoingHandler func(req OutgoingCallMsg) OutgoingCallAck

// Config holds the bridge server configuration.
type Config struct {
	Enabled bool   `yaml:"enabled"`
	Listen  string `yaml:"listen"` // "127.0.0.1:9090"
	Path    string `yaml:"path"`   // "/ws"
}

// DefaultConfig returns sensible defaults for the bridge server.
func DefaultConfig() Config {
	return Config{
		Enabled: false,
		Listen:  "127.0.0.1:9090",
		Path:    "/ws",
	}
}

// NewServer creates a bridge server from config. Does not start listening yet.
func NewServer(cfg Config) *Server {
	return &Server{
		listen: cfg.Listen,
		path:   cfg.Path,
	}
}

// Start begins listening for WebSocket connections. It blocks until the
// server is shut down via Stop() or the context is cancelled.
func (s *Server) Start(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc(s.path, s.handleUpgrade)
	mux.HandleFunc("/api/call", s.handleOutgoingCall)

	s.srv = &http.Server{
		Addr:    s.listen,
		Handler: mux,
		BaseContext: func(_ net.Listener) context.Context {
			return ctx
		},
	}

	ln, err := net.Listen("tcp", s.listen)
	if err != nil {
		return err
	}
	s.ln = ln

	log.Printf("[bridge] listening on %s path=%s", s.listen, s.path)

	go func() {
		if err := s.srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Printf("[bridge] serve error: %v", err)
		}
	}()

	<-ctx.Done()
	return s.Stop()
}

// Stop gracefully shuts down the server and disconnects any active agent.
func (s *Server) Stop() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	if s.agent != nil {
		s.agent.Close()
		s.agent = nil
	}
	s.mu.Unlock()

	if s.srv != nil {
		return s.srv.Close()
	}
	return nil
}

// handleUpgrade upgrades HTTP to WebSocket and registers the agent.
func (s *Server) handleUpgrade(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		http.Error(w, "server shutting down", http.StatusServiceUnavailable)
		return
	}
	if s.agent != nil {
		s.mu.Unlock()
		http.Error(w, "agent already connected", http.StatusConflict)
		return
	}
	s.mu.Unlock()

	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		OriginPatterns: []string{"*"}, // POC: allow all origins
	})
	if err != nil {
		log.Printf("[bridge] websocket accept failed: %v", err)
		return
	}

	remoteAddr := r.RemoteAddr
	if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
		remoteAddr = fwd
	}
	// Use a background context — the request context is cancelled when the
	// HTTP handler returns, but we need the agent connection to outlive it.
	agent := NewAgentConn(context.Background(), conn, remoteAddr)
	log.Printf("[bridge] agent connected: %s", agent.RemoteAddr())

	s.mu.Lock()
	s.agent = agent
	s.mu.Unlock()

	// Watch for agent disconnect.
	go func() {
		<-agent.Done()
		s.mu.Lock()
		if s.agent == agent {
			s.agent = nil
			log.Printf("[bridge] agent disconnected: %s", agent.RemoteAddr())
		}
		s.mu.Unlock()
	}()

	// Single controls dispatcher for the whole connection. One goroutine
	// owns the controls channel and routes each message to the AgentSource
	// registered for its callID. This is safe for any number of sequential
	// or concurrent calls, and a control for a finished call is simply
	// dropped (no source registered) instead of being stolen by a stale
	// per-call goroutine.
	go func() {
		for ctrl := range agent.Controls() {
			v, ok := s.agentSrcs.Load(ctrl.CallID)
			if !ok {
				// Call not (or no longer) active — nothing to control.
				continue
			}
			src, ok := v.(*AgentSource)
			if !ok {
				continue
			}
			switch ctrl.Type {
			case "audio_stop":
				log.Printf("[bridge] audio_stop received for call %s — flushing bot audio", ctrl.CallID)
				src.StopPlayback()
			case "audio_playing":
				src.ResumePlayback()
			}
		}
	}()

	// Stale-agent watchdog: if the agent process dies without a clean
	// WebSocket close (e.g. SIGKILL), the reader goroutine never errors and
	// s.agent stays set forever, rejecting all new agents with HTTP 409.
	// Ping the agent periodically; if it does not answer, force-close the
	// connection so a replacement agent can connect.
	go s.pingWatchdog(agent)
}

// pingWatchdog keeps the agent connection honest. It sends a WebSocket ping
// every pingInterval; if the agent fails to answer within pingTimeout the
// connection is considered dead and closed, which unblocks handleUpgrade's
// disconnect watcher above.
//
// The watchdog is traffic-aware: if the agent has sent ANY message (audio
// frame, control event, etc.) within the last pingInterval, it is provably
// alive and the ping is skipped. This prevents false disconnects when the
// agent's event loop is briefly busy (e.g. finalizing a call) and answers
// pongs late. A ping only runs when the agent has been completely silent.
func (s *Server) pingWatchdog(agent *AgentConn) {
	const (
		pingInterval = 15 * time.Second
		pingTimeout  = 10 * time.Second
	)
	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-agent.Done():
			return
		case <-ticker.C:
			// Traffic within the last interval proves liveness — skip ping.
			if time.Since(agent.LastRecv()) < pingInterval {
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), pingTimeout)
			err := agent.conn.Ping(ctx)
			cancel()
			if err != nil {
				log.Printf("[bridge] agent ping failed (%v) — closing stale connection %s", err, agent.RemoteAddr())
				_ = agent.Close()
				return
			}
		}
	}
}

// SetOutgoingHandler registers the outbound call handler (wired by main to the
// outgoing.Manager). Until set, POST /api/call returns 503.
func (s *Server) SetOutgoingHandler(h OutgoingHandler) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.outgoingHandler = h
}

// handleOutgoingCall is the HTTP endpoint for outbound call requests.
//
//	POST /api/call
//	{"type":"outgoing_call","phone":"62812...","audio":"pesan.wav","delay_ms":2000}
//
// Response: 200 with {"status":"accepted","call_id":...} when the call is
// placed, or 409 with {"status":"rejected","reason":...} when the request is
// invalid (bad format, not allowlisted, rate limited, missing audio file).
func (s *Server) handleOutgoingCall(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"status":"rejected","reason":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	s.mu.Lock()
	h := s.outgoingHandler
	s.mu.Unlock()
	if h == nil {
		writeJSON(w, http.StatusServiceUnavailable, OutgoingCallAck{
			Type: "outgoing_call_ack", Status: "rejected", Reason: "outgoing calls disabled",
		})
		return
	}

	var req OutgoingCallMsg
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, OutgoingCallAck{
			Type: "outgoing_call_ack", Status: "rejected", Reason: "invalid JSON body",
		})
		return
	}
	req.Type = "outgoing_call" // normalize

	ack := h(req)
	if ack.Status == "accepted" {
		writeJSON(w, http.StatusOK, ack)
		return
	}
	writeJSON(w, http.StatusConflict, ack)
}

// writeJSON writes v as a JSON response with the given status code.
func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("[bridge] write JSON response failed: %v", err)
	}
}

// SetAgentSource registers the AgentSource for an active call so the
// connection-wide controls dispatcher can route audio_playing/audio_stop
// messages to it. Call this right after creating the AgentSource and
// ClearAgentSource (defer) when the call ends.
func (s *Server) SetAgentSource(callID string, src *AgentSource) {
	s.agentSrcs.Store(callID, src)
}

// ClearAgentSource removes the AgentSource for a finished call.
func (s *Server) ClearAgentSource(callID string) {
	s.agentSrcs.Delete(callID)
}

// Agent returns the current agent connection, or nil if none is connected.
func (s *Server) Agent() *AgentConn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.agent
}

// HasAgent returns true if an agent is currently connected.
func (s *Server) HasAgent() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.agent != nil
}

// WaitForAgent blocks until an agent connects or the timeout/context expires.
// Returns the agent connection or nil on timeout.
func (s *Server) WaitForAgent(timeout time.Duration) *AgentConn {
	deadline := time.After(timeout)
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-deadline:
			return nil
		case <-ticker.C:
			if a := s.Agent(); a != nil {
				return a
			}
		}
	}
}
