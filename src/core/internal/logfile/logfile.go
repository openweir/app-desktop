// Package logfile writes a log that moves aside whenever it outgrows its limit, not only
// when its program starts: the Windows service runs for months, and anything that makes
// it log, such as a client connecting again and again, would otherwise fill the disk
// (docs/architecture.md §5.1).
package logfile

import (
	"os"
	"sync"
)

// File is a log of at most limit bytes, with the one before it at path.1.
type File struct {
	path  string
	limit int64

	mu     sync.Mutex
	file   *os.File
	closed bool
	// size is what file holds, or what was written since a move aside failed.
	size int64
}

// Open appends to the log at path. Once a write would take it past limit, the log moves to
// path.1, replacing what was there, and starts over; a log already past limit moves at the
// first write.
func Open(path string, limit int64) (*File, error) {
	f := &File{path: path, limit: limit}
	if err := f.open(0); err != nil {
		return nil, err
	}
	info, err := f.file.Stat()
	if err != nil {
		f.file.Close()
		return nil, err
	}
	f.size = info.Size()
	return f, nil
}

func (f *File) open(flags int) error {
	file, err := os.OpenFile(f.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY|flags, 0o600)
	if err != nil {
		return err
	}
	f.file = file
	return nil
}

func (f *File) Write(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		return 0, os.ErrClosed
	}
	if f.size > 0 && f.size+int64(len(p)) > f.limit {
		f.rotate()
	}
	if f.file == nil {
		if err := f.open(0); err != nil {
			return 0, err
		}
	}
	n, err := f.file.Write(p)
	f.size += int64(n)
	return n, err
}

// rotate moves the log aside. Another program may hold it open so that it cannot move, an
// editor say; then it starts over in place, and failing that it grows by another limit
// before the next try.
func (f *File) rotate() {
	f.file.Close()
	f.file = nil
	f.size = 0
	if err := os.Rename(f.path, f.path+".1"); err == nil {
		_ = f.open(0)
	} else if err := f.open(os.O_TRUNC); err != nil {
		_ = f.open(0)
	}
}

// Close closes the log.
func (f *File) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = true
	if f.file == nil {
		return nil
	}
	err := f.file.Close()
	f.file = nil
	return err
}
