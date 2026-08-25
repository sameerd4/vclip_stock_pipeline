import Foundation
import Vision
import ImageIO
import CoreGraphics

struct Output: Codable {
    let count: Int
    let distances: [[Float]]
}

func loadCGImage(_ path: String) throws -> CGImage {
    let url = URL(fileURLWithPath: path) as CFURL
    guard let source = CGImageSourceCreateWithURL(url, nil) else {
        throw NSError(domain: "VClipVision", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "Cannot open image source: \(path)"
        ])
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "VClipVision", code: 2, userInfo: [
            NSLocalizedDescriptionKey: "Cannot decode image: \(path)"
        ])
    }
    return image
}

func featurePrint(_ image: CGImage) throws -> VNFeaturePrintObservation {
    let request = VNGenerateImageFeaturePrintRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    guard let observation = request.results?.first as? VNFeaturePrintObservation else {
        throw NSError(domain: "VClipVision", code: 3, userInfo: [
            NSLocalizedDescriptionKey: "Vision returned no feature print"
        ])
    }
    return observation
}

do {
    let paths = Array(CommandLine.arguments.dropFirst())
    guard !paths.isEmpty else {
        throw NSError(domain: "VClipVision", code: 4, userInfo: [
            NSLocalizedDescriptionKey: "Usage: vclip-vision-featureprint IMAGE..."
        ])
    }

    var observations: [VNFeaturePrintObservation] = []
    observations.reserveCapacity(paths.count)

    for path in paths {
        let image = try loadCGImage(path)
        observations.append(try featurePrint(image))
    }

    var matrix = Array(
        repeating: Array(repeating: Float(0), count: observations.count),
        count: observations.count
    )

    if observations.count > 1 {
        for i in 0..<(observations.count - 1) {
            for j in (i + 1)..<observations.count {
                var distance: Float = 0
                try observations[i].computeDistance(&distance, to: observations[j])
                matrix[i][j] = distance
                matrix[j][i] = distance
            }
        }
    }

    let payload = Output(count: observations.count, distances: matrix)
    let data = try JSONEncoder().encode(payload)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
} catch {
    fputs("vclip-vision-featureprint: \(error)\n", stderr)
    exit(1)
}
