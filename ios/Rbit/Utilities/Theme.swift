import SwiftUI
import UIKit

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: alpha
        )
    }

    init(light: Color, dark: Color) {
        let lightColor = UIColor(light)
        let darkColor = UIColor(dark)
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? darkColor : lightColor
        })
    }

    static let rbitBackground = Color(light: Color(hex: 0xFFFFFF), dark: Color(hex: 0x0A0A0A))
    static let rbitSurface = Color(light: Color(hex: 0xF6F6F6), dark: Color(hex: 0x141414))
    static let rbitPanel = Color(light: Color(hex: 0xEDEDED), dark: Color(hex: 0x1E1E1E))
    static let rbitPanelRaised = Color(light: Color(hex: 0xE3E3E3), dark: Color(hex: 0x262626))
    static let rbitBorder = Color(light: Color(hex: 0xE2E2E2), dark: Color(hex: 0x2A2A2A))
    static let rbitText = Color(light: Color(hex: 0x1A1A1A), dark: Color(hex: 0xEEEEEE))
    static let rbitDim = Color(light: Color(hex: 0x6B6B6B), dark: Color(hex: 0x8A8A8A))
    static let rbitFaint = Color(light: Color(hex: 0x9A9A9A), dark: Color(hex: 0x5A5A5A))
    static let rbitWordmark = Color(light: Color(hex: 0xE6E6E6), dark: Color(hex: 0x1D1D1D))
    static let rbitCodeBackground = Color(light: Color(hex: 0xF0F0F0), dark: Color(hex: 0x101010))

    static let rbitAccent = Color(light: Color(hex: 0xD97706), dark: Color(hex: 0xFAB283))
    static let rbitAccentSoft = Color(light: Color(hex: 0xD97706).opacity(0.12), dark: Color(hex: 0xFAB283).opacity(0.14))
    static let rbitBlue = Color(light: Color(hex: 0x2563EB), dark: Color(hex: 0x5C9CF5))
    static let rbitBlueSoft = Color(light: Color(hex: 0x2563EB).opacity(0.12), dark: Color(hex: 0x5C9CF5).opacity(0.14))
    static let rbitViolet = Color(light: Color(hex: 0x7C3AED), dark: Color(hex: 0x9D7CD8))
    static let rbitGreen = Color(light: Color(hex: 0x16A34A), dark: Color(hex: 0x7FD88F))
    static let rbitRed = Color(light: Color(hex: 0xDC2626), dark: Color(hex: 0xE06C75))
    static let rbitYellow = Color(light: Color(hex: 0xD97706), dark: Color(hex: 0xF5A742))

    static let rbitGlass = Color(light: Color.white.opacity(0.72), dark: Color(hex: 0x141414).opacity(0.76))
    static let rbitGlassBorder = Color(light: Color.black.opacity(0.08), dark: Color.white.opacity(0.12))
}

enum RbitFont {
    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static func ui(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .default)
    }
}

struct RbitCardModifier: ViewModifier {
    var padding: CGFloat = 16
    var radius: CGFloat = 18
    var fill: Color = .rbitSurface

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(fill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )
    }
}

extension View {
    func rbitCard(
        padding: CGFloat = 16,
        radius: CGFloat = 18,
        fill: Color = .rbitSurface
    ) -> some View {
        modifier(RbitCardModifier(padding: padding, radius: radius, fill: fill))
    }
}

struct PressableButtonStyle: ButtonStyle {
    var scale: CGFloat = 0.975

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .opacity(configuration.isPressed ? 0.82 : 1)
            .animation(.easeOut(duration: 0.14), value: configuration.isPressed)
    }
}

struct RbitPrimaryButtonStyle: ButtonStyle {
    var tint: Color = .rbitAccent
    var height: CGFloat = 48

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(RbitFont.ui(15, weight: .semibold))
            .foregroundStyle(Color(hex: 0x141414))
            .frame(maxWidth: .infinity)
            .frame(height: height)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(tint)
            )
            .opacity(configuration.isPressed ? 0.8 : 1)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.14), value: configuration.isPressed)
    }
}

struct RbitGhostButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(RbitFont.ui(14, weight: .medium))
            .foregroundStyle(Color.rbitText)
            .padding(.horizontal, 14)
            .frame(height: 40)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.rbitPanel)
            )
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.72 : 1)
    }
}

struct RbitDivider: View {
    var body: some View {
        Rectangle()
            .fill(Color.rbitBorder)
            .frame(height: 1)
    }
}

struct DotGridBackground: View {
    var spacing: CGFloat = 28
    var opacity: Double = 0.045

    var body: some View {
        Canvas { context, size in
            let color = Color.rbitText.opacity(opacity)
            var x: CGFloat = 0
            while x < size.width {
                var y: CGFloat = 0
                while y < size.height {
                    let rect = CGRect(x: x, y: y, width: 2, height: 2)
                    context.fill(Path(ellipseIn: rect), with: .color(color))
                    y += spacing
                }
                x += spacing
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

struct RbitScreenBackground: View {
    var dots: Bool = false

    var body: some View {
        ZStack {
            Color.rbitBackground
            RadialGradient(
                colors: [Color.rbitAccent.opacity(0.055), Color.clear],
                center: .topTrailing,
                startRadius: 20,
                endRadius: 460
            )
            if dots {
                DotGridBackground()
            }
        }
        .ignoresSafeArea()
    }
}

struct GlassCapsule: ViewModifier {
    var radius: CGFloat = 18

    func body(content: Content) -> some View {
        content
            .background(.ultraThinMaterial)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(Color.rbitGlass)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Color.rbitGlassBorder, lineWidth: 1)
            )
    }
}

extension View {
    func glassCapsule(radius: CGFloat = 18) -> some View {
        modifier(GlassCapsule(radius: radius))
    }
}
