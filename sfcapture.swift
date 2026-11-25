// sfcapture: records one display with ScreenCaptureKit, leaving out the windows of one process (screenfeed's
// own widget, recording frame and badges), and writes the frames as raw NV12 at a fixed rate to a file or FIFO,
// where ffmpeg reads them. The cursor is drawn into the frames.
//
// usage: sfcapture --display <CGDirectDisplayID> --width <px> --height <px> --fps <n> --exclude-pid <pid> --out <path>
//
// ScreenCaptureKit only sends a new frame when the screen changes, so the last frame is written again at every
// tick: ffmpeg gets a steady stream and its frame counter keeps moving, which is what screenfeed's watchdog
// watches. Exits 0 when the reader goes away, 1 with a message on stderr when it cannot capture.

import CoreMedia
import CoreVideo
import Foundation
import ScreenCaptureKit

setvbuf(stderr, nil, _IONBF, 0)
signal(SIGPIPE, SIG_IGN)

func fail(_ message: String) -> Never {
  FileHandle.standardError.write(("sfcapture: " + message + "\n").data(using: .utf8)!)
  exit(1)
}

func argument(_ name: String) -> String? {
  let args = CommandLine.arguments
  guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
  return args[i + 1]
}

guard let displayID = UInt32(argument("--display") ?? ""),
  let width = Int(argument("--width") ?? ""), let height = Int(argument("--height") ?? ""),
  let outPath = argument("--out")
else { fail("usage: sfcapture --display ID --width W --height H --fps N --exclude-pid PID --out PATH") }
let fps = Int(argument("--fps") ?? "5") ?? 5
let excludePid = Int32(argument("--exclude-pid") ?? "-1") ?? -1

final class Frames: NSObject, SCStreamOutput, SCStreamDelegate {
  private let lock = NSLock()
  private var latestBuffer: CVPixelBuffer?
  private(set) var received = 0

  var latest: CVPixelBuffer? {
    lock.lock()
    defer { lock.unlock() }
    return latestBuffer
  }

  func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
    guard type == .screen, sampleBuffer.isValid, let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
    // Only complete frames carry a picture; idle ones (nothing changed) do not.
    if let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
      as? [[SCStreamFrameInfo: Any]],
      let raw = attachments.first?[.status] as? Int, let status = SCFrameStatus(rawValue: raw), status != .complete
    {
      return
    }
    lock.lock()
    latestBuffer = buffer
    received += 1
    lock.unlock()
  }

  func stream(_ stream: SCStream, didStopWithError error: Error) {
    fail("capture stopped: \(error.localizedDescription)")
  }
}

/// Writes one NV12 frame row by row: the buffer's rows may be padded beyond the picture's width.
func write(_ buffer: CVPixelBuffer, to out: UnsafeMutablePointer<FILE>) -> Bool {
  CVPixelBufferLockBaseAddress(buffer, .readOnly)
  defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
  for plane in 0..<2 {
    guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, plane) else { return true }
    let bytesPerRow = CVPixelBufferGetBytesPerRowOfPlane(buffer, plane)
    let available = CVPixelBufferGetHeightOfPlane(buffer, plane)
    let rows = plane == 0 ? height : height / 2
    let rowBytes = min(width, bytesPerRow)  // luma: width bytes; chroma: width/2 interleaved pairs
    for row in 0..<rows {
      let source = base + min(row, available - 1) * bytesPerRow
      if fwrite(source, 1, rowBytes, out) != rowBytes { return false }
    }
  }
  return fflush(out) == 0
}

// What there is to capture. Needs the Screen Recording permission of the app screenfeed was started from.
let ready = DispatchSemaphore(value: 0)
var shareable: SCShareableContent?
var shareableError: Error?
SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
  shareable = content
  shareableError = error
  ready.signal()
}
ready.wait()
guard let content = shareable else {
  fail("no screen content (Screen Recording permission?): \(shareableError?.localizedDescription ?? "unknown")")
}
guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
  fail("display \(displayID) not found among \(content.displays.map { $0.displayID })")
}
// Leaving out the whole application also leaves out the windows it opens later (frames come and go).
let excluded = content.applications.filter { $0.processID == excludePid }
let filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])

let configuration = SCStreamConfiguration()
configuration.width = width
configuration.height = height
configuration.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
configuration.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
configuration.showsCursor = true
configuration.queueDepth = 5

let frames = Frames()
let stream = SCStream(filter: filter, configuration: configuration, delegate: frames)
do {
  try stream.addStreamOutput(frames, type: .screen, sampleHandlerQueue: DispatchQueue(label: "sfcapture.frames"))
} catch {
  fail("cannot add stream output: \(error.localizedDescription)")
}
let started = DispatchSemaphore(value: 0)
var startError: Error?
stream.startCapture { error in
  startError = error
  started.signal()
}
started.wait()
if let startError { fail("cannot start capture: \(startError.localizedDescription)") }
FileHandle.standardError.write(
  "sfcapture: display \(displayID) at \(width)x\(height), \(fps) fps, leaving out \(excluded.count) app(s) of pid \(excludePid)\n"
    .data(using: .utf8)!)

// Opening a FIFO blocks until the reader (ffmpeg) has opened it.
guard let out = fopen(outPath, "w") else { fail("cannot open \(outPath)") }
let interval = 1.0 / Double(fps)
var next = Date().timeIntervalSince1970
while true {
  if let buffer = frames.latest, !write(buffer, to: out) {
    exit(0)  // the reader is gone: recording stopped
  }
  next += interval
  let wait = next - Date().timeIntervalSince1970
  if wait > 0 {
    Thread.sleep(forTimeInterval: wait)
  } else {
    next = Date().timeIntervalSince1970  // fell behind (e.g. after sleep): start over instead of catching up
  }
}
