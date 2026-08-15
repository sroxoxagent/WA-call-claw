package recorder

import (
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/purpshell/meowcaller"
)

// WAVRecorder wraps meowcaller.WAVRecorder with lifecycle management and
// instrumentation (frame count, PCM RMS level tracking).
type WAVRecorder struct {
	sink     meowcaller.AudioSink
	path     string
	mu       sync.Mutex
	started  time.Time
	finished bool

	// Instrumentation counters (protected by mu).
	frameCount   uint64
	lastRMS      float64
	sumSquared   float64 // for cumulative RMS
	totalSamples uint64
}

// NewWAVRecorder creates a new WAV recorder at the given path.
func NewWAVRecorder(path string) (*WAVRecorder, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("create recording dir: %w", err)
	}

	sink, err := meowcaller.WAVRecorder(path)
	if err != nil {
		return nil, fmt.Errorf("create WAV recorder: %w", err)
	}

	return &WAVRecorder{
		sink:    sink,
		path:    path,
		started: time.Now(),
	}, nil
}

// Sink returns the raw meowcaller AudioSink (no instrumentation).
func (r *WAVRecorder) Sink() meowcaller.AudioSink {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sink
}

// InstrumentedSink returns a wrapper AudioSink that tracks frame count and
// PCM RMS level while delegating WriteFrame/Close to the underlying WAV sink.
// Use this with call.Receive() for automatic instrumentation.
func (r *WAVRecorder) InstrumentedSink() meowcaller.AudioSink {
	return &instrumentedSink{rec: r}
}

// Finish closes the underlying sink so the WAV header is finalized, then logs the result.
func (r *WAVRecorder) Finish() error {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.finished {
		return nil
	}

	if err := r.sink.Close(); err != nil {
		log.Printf("WAV recorder: close failed for %s: %v", r.path, err)
		return err
	}
	r.finished = true

	info, err := os.Stat(r.path)
	if err != nil {
		log.Printf("WAV recorder: cannot stat file %s: %v", r.path, err)
		return err
	}

	log.Printf("WAV recorder: finalized %s (%d bytes, frames=%d, duration since start: %v)",
		r.path, info.Size(), r.frameCount, time.Since(r.started).Truncate(time.Millisecond))
	return nil
}

// Path returns the file path of the recording.
func (r *WAVRecorder) Path() string {
	return r.path
}

// IsFinished returns whether the recorder has been finalized.
func (r *WAVRecorder) IsFinished() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.finished
}

// FrameCount returns the number of audio frames written.
func (r *WAVRecorder) FrameCount() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.frameCount
}

// LastRMS returns the RMS level of the most recently written frame.
func (r *WAVRecorder) LastRMS() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.lastRMS
}

// DurationMs returns the elapsed time since the recorder was created, in milliseconds.
func (r *WAVRecorder) DurationMs() int64 {
	return time.Since(r.started).Milliseconds()
}

// LogSummary logs instrumentation data for the call.
func (r *WAVRecorder) LogSummary(callID string) {
	r.mu.Lock()
	frames := r.frameCount
	rms := r.lastRMS
	avgRMS := 0.0
	if r.totalSamples > 0 {
		avgRMS = math.Sqrt(r.sumSquared / float64(r.totalSamples))
	}
	r.mu.Unlock()

	log.Printf("call %s instrumentation: frames=%d last_rms=%.6f avg_rms=%.6f duration=%dms",
		callID, frames, rms, avgRMS, r.DurationMs())
}

// writeFrameLocked must be called with r.mu held. Updates instrumentation counters.
func (r *WAVRecorder) writeFrameLocked(frame []float32) error {
	r.frameCount++

	// Compute per-frame RMS.
	var sumSq float64
	for _, s := range frame {
		sumSq += float64(s) * float64(s)
	}
	rms := math.Sqrt(sumSq / float64(len(frame)))
	r.lastRMS = rms
	r.sumSquared += sumSq
	r.totalSamples += uint64(len(frame))

	return r.sink.WriteFrame(frame)
}

// instrumentedSink wraps a WAVRecorder and delegates WriteFrame/Close to it,
// while updating the recorder's instrumentation counters.
type instrumentedSink struct {
	rec *WAVRecorder
}

func (s *instrumentedSink) WriteFrame(frame []float32) error {
	s.rec.mu.Lock()
	defer s.rec.mu.Unlock()
	return s.rec.writeFrameLocked(frame)
}

func (s *instrumentedSink) Close() error {
	// Close is called by Finish(); do not double-close here.
	return nil
}
