// On-device text recognition via Apple's Vision framework.
//
// Why a Swift shim rather than a Python OCR package: Vision is already on every
// macOS box, needs no model download, costs nothing per image, and reads burned-in
// reel captions better than Tesseract does. The alternative was sending frames to a
// vision model at roughly $0.0002 each, which is both slower and unnecessary.
//
// Reads image paths from argv, writes one JSON object per line to stdout:
//   {"path": "...", "lines": [{"text": "...", "confidence": 1.0}]}
// A file it cannot read produces a line with "error" set rather than killing the run,
// because this is fed a whole batch of keyframes and one bad frame must not lose the rest.
import Foundation
import Vision
import AppKit

let args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty else {
    FileHandle.standardError.write("usage: ocr <image>...\n".data(using: .utf8)!)
    exit(2)
}

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
          let s = String(data: data, encoding: .utf8) else { return }
    print(s)
}

for path in args {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        emit(["path": path, "error": "unreadable"])
        continue
    }

    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    // Vision ships no Croatian, but the Latin-script recogniser reads it anyway.
    // Language correction against these locales is close enough not to mangle it,
    // and verified on real reels: "MOZES ZA", "VAZE 80%" came back intact.
    req.recognitionLanguages = ["en-US", "de-DE", "es-ES", "it-IT"]
    // Reel captions are large; ignoring tiny text drops watermarks and UI chrome.
    req.minimumTextHeight = 0.02

    do {
        try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    } catch {
        emit(["path": path, "error": "\(error)"])
        continue
    }

    let lines: [[String: Any]] = (req.results ?? []).compactMap { obs in
        guard let top = obs.topCandidates(1).first else { return nil }
        return ["text": top.string, "confidence": Double(top.confidence)]
    }
    emit(["path": path, "lines": lines])
}
