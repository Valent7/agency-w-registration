import Foundation
import CoreVideo
import Vision

func sendMasks(width: Int, height: Int, output: URL) throws {
    guard width > 0, height > 0 else { throw CocoaError(.fileReadCorruptFile) }
    let frameBytes = width * height * 4
    let sequence = VNSequenceRequestHandler()
    let request = VNGeneratePersonSegmentationRequest()
    request.qualityLevel = .balanced
    request.outputPixelFormat = kCVPixelFormatType_OneComponent8
    FileManager.default.createFile(atPath: output.path, contents: nil)
    let file = try FileHandle(forWritingTo: output)
    defer { try? file.close() }

    var buffer: CVPixelBuffer?
    guard CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, nil, &buffer) == kCVReturnSuccess,
          let image = buffer else { throw CocoaError(.fileReadUnknown) }
    let input = FileHandle.standardInput
    var maskWidth = 0
    var maskHeight = 0
    var count = 0

    while var pixels = try input.read(upToCount: frameBytes), !pixels.isEmpty {
        while pixels.count < frameBytes {
            guard let next = try input.read(upToCount: frameBytes - pixels.count), !next.isEmpty else {
                throw CocoaError(.fileReadCorruptFile)
            }
            pixels.append(next)
        }
        try autoreleasepool {
            CVPixelBufferLockBaseAddress(image, [])
            let stride = CVPixelBufferGetBytesPerRow(image)
            if let base = CVPixelBufferGetBaseAddress(image) {
                pixels.withUnsafeBytes { bytes in
                    guard let source = bytes.baseAddress else { return }
                    for row in 0..<height {
                        memcpy(base.advanced(by: row * stride), source.advanced(by: row * width * 4), width * 4)
                    }
                }
            }
            CVPixelBufferUnlockBaseAddress(image, [])
            try sequence.perform([request], on: image)
            guard let mask = request.results?.first?.pixelBuffer else { throw CocoaError(.fileReadCorruptFile) }
            CVPixelBufferLockBaseAddress(mask, .readOnly)
            defer { CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
            maskWidth = CVPixelBufferGetWidth(mask)
            maskHeight = CVPixelBufferGetHeight(mask)
            let maskStride = CVPixelBufferGetBytesPerRow(mask)
            guard let base = CVPixelBufferGetBaseAddress(mask) else { throw CocoaError(.fileReadCorruptFile) }
            var matte = Data(capacity: maskWidth * maskHeight)
            for row in 0..<maskHeight {
                matte.append(base.advanced(by: row * maskStride).assumingMemoryBound(to: UInt8.self), count: maskWidth)
            }
            try file.write(contentsOf: matte)
            count += 1
        }
    }
    guard count > 0 else { throw CocoaError(.fileReadCorruptFile) }
    print("\(maskWidth) \(maskHeight) \(count)")
}

do {
    let args = CommandLine.arguments
    guard args.count == 4, let width = Int(args[1]), let height = Int(args[2]) else {
        throw CocoaError(.fileReadInvalidFileName)
    }
    try sendMasks(width: width, height: height, output: URL(fileURLWithPath: args[3]))
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
