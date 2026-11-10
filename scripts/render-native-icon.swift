// Rasterizes the approved assets/trace-icon.svg geometry for native packaging.
// Run from the repository root: swift scripts/render-native-icon.swift
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
func color(_ hex: UInt32) -> CGColor {
    CGColor(red: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: 1)
}
let cream = color(0xfff3db), outline = color(0x482916), pages = color(0xfff0cb)
func drawMark(_ context: CGContext) {
    context.setStrokeColor(outline)
    context.setLineWidth(2.4)
    context.setLineJoin(.round)
    func rounded(_ rect: CGRect, _ radius: CGFloat, _ fill: CGColor) {
        context.addPath(CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil))
        context.setFillColor(fill)
        context.drawPath(using: .fillStroke)
    }
    rounded(CGRect(x: 14, y: 4, width: 13, height: 32), 4, color(0xdf703e))
    let topPages = CGMutablePath()
    topPages.move(to: CGPoint(x: 21, y: 5)); topPages.addLine(to: CGPoint(x: 24, y: 5))
    topPages.addQuadCurve(to: CGPoint(x: 26, y: 7), control: CGPoint(x: 26, y: 5))
    topPages.addLine(to: CGPoint(x: 26, y: 12)); topPages.addLine(to: CGPoint(x: 21, y: 12)); topPages.closeSubpath()
    context.addPath(topPages); context.setFillColor(pages); context.drawPath(using: .fillStroke)
    rounded(CGRect(x: 4, y: 16, width: 32, height: 11), 4, color(0x4d9290))
    let sidePages = CGMutablePath()
    sidePages.move(to: CGPoint(x: 29, y: 17)); sidePages.addLine(to: CGPoint(x: 32, y: 17))
    sidePages.addQuadCurve(to: CGPoint(x: 35, y: 20), control: CGPoint(x: 35, y: 17))
    sidePages.addLine(to: CGPoint(x: 35, y: 23))
    sidePages.addQuadCurve(to: CGPoint(x: 32, y: 26), control: CGPoint(x: 35, y: 26))
    sidePages.addLine(to: CGPoint(x: 29, y: 26)); sidePages.closeSubpath()
    context.addPath(sidePages); context.drawPath(using: .fillStroke)
    context.setLineWidth(1.2); context.setLineCap(.round)
    for (x1, y1, x2, y2): (CGFloat, CGFloat, CGFloat, CGFloat) in [(23,7,23,10), (31,20,33,20), (31,23,33,23)] {
        context.move(to: CGPoint(x: x1, y: y1)); context.addLine(to: CGPoint(x: x2, y: y2)); context.strokePath()
    }
}
func render(_ path: String, size: Int, markSize: CGFloat? = nil) throws {
    guard let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
        bytesPerRow: size * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
        throw NSError(domain: "TraceIcon", code: 1)
    }
    context.setFillColor(cream); context.fill(CGRect(x: 0, y: 0, width: size, height: size))
    context.translateBy(x: 0, y: CGFloat(size)); context.scaleBy(x: 1, y: -1)
    let edge = markSize ?? CGFloat(size)
    let inset = (CGFloat(size) - edge) / 2
    context.translateBy(x: inset, y: inset); context.scaleBy(x: edge / 40, y: edge / 40)
    drawMark(context)
    guard let image = context.makeImage(), let destination = CGImageDestinationCreateWithURL(root.appendingPathComponent(path) as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        throw NSError(domain: "TraceIcon", code: 2)
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw NSError(domain: "TraceIcon", code: 3) }
}
try render("native/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png", size: 1024)
for name in ["splash-2732x2732.png", "splash-2732x2732-1.png", "splash-2732x2732-2.png"] {
    try render("native/ios/App/App/Assets.xcassets/Splash.imageset/" + name, size: 2732, markSize: 340)
}
for (density, size) in [("mdpi",48), ("hdpi",72), ("xhdpi",96), ("xxhdpi",144), ("xxxhdpi",192)] {
    for name in ["ic_launcher.png", "ic_launcher_round.png"] {
        try render("native/android/app/src/main/res/mipmap-" + density + "/" + name, size: size)
    }
}
print("Rendered Trace iOS/Android launcher icons and iOS launch images.")
