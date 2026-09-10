// Local OCR to constrain crops around existing burnt-in captions. No subtitles generated.
import Foundation
import Vision
import AVFoundation

let args = CommandLine.arguments
guard args.count == 5, let start = Double(args[2]), let end = Double(args[3]), start < end else {
    fputs("usage: caption-bounds video start end output.json\n", stderr)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.maximumSize = CGSize(width: 1280, height: 1280)
generator.requestedTimeToleranceBefore = CMTime(seconds: 0.05, preferredTimescale: 600)
generator.requestedTimeToleranceAfter = CMTime(seconds: 0.05, preferredTimescale: 600)
var results: [[String: Any]] = []
var time = start + 0.1
while time < end {
    autoreleasepool {
        do {
            let image = try generator.copyCGImage(at: CMTime(seconds: time, preferredTimescale: 600), actualTime: nil)
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["ko-KR", "en-US"]
            request.usesLanguageCorrection = false
            request.minimumTextHeight = 0.012
            request.regionOfInterest = CGRect(x: 0, y: 0, width: 1, height: 0.35)
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
            let boxes = (request.results ?? []).compactMap { observation -> [String: Any]? in
                guard let text = observation.topCandidates(1).first, text.confidence >= 0.3,
                      text.string.count >= 3 else { return nil }
                let b = observation.boundingBox
                // Vision reports coordinates relative to the ROI. Retain large text
                // in the bottom ~16% of the full frame, excluding bookshelf labels.
                guard b.minY < 0.45, b.height >= 0.07 else { return nil }
                return ["left": max(0, b.minX), "right": min(1, b.maxX), "bottom": b.minY * 0.35,
                        "top": b.maxY * 0.35, "text": text.string, "confidence": text.confidence]
            }
            results.append(["time": time, "boxes": boxes])
        } catch {
            results.append(["time": time, "boxes": [], "error": "frame OCR failed"])
        }
    }
    if results.count % 10 == 0 {
        print("OCR \(Int((time-start)/(end-start)*100))%")
        fflush(stdout)
    }
    time += 1.0
}
do {
    let data = try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
    try data.write(to: URL(fileURLWithPath: args[4]), options: .atomic)
} catch {
    fputs("caption result write failed\n", stderr)
    exit(1)
}
