import SwiftUI
import UIKit

struct BrandMark: View {
    var size: CGFloat = 22
    var color: Color = .rbitText
    var accent: Color = .rbitAccent

    var body: some View {
        HStack(alignment: .top, spacing: max(2, size * 0.12)) {
            Text("rbit")
                .font(.system(size: size, weight: .bold, design: .monospaced))
                .foregroundStyle(color)
            Rectangle()
                .fill(accent)
                .frame(width: size * 0.22, height: size * 0.22)
                .padding(.top, size * 0.07)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("rbit")
    }
}

struct HeroWordmark: View {
    var size: CGFloat = 88

    var body: some View {
        HStack(alignment: .top, spacing: size * 0.06) {
            Text("rbit")
                .font(.system(size: size, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.rbitWordmark)
            Rectangle()
                .fill(Color.rbitAccent)
                .frame(width: size * 0.16, height: size * 0.16)
                .padding(.top, size * 0.02)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("rbit")
    }
}

struct ConnectionPill: View {
    @EnvironmentObject private var app: AppState

    var action: (() -> Void)? = nil

    private var color: Color {
        switch app.connection {
        case .online: return .rbitGreen
        case .connecting: return .rbitYellow
        case .failed: return .rbitRed
        case .disconnected: return .rbitDim
        }
    }

    private var text: String {
        switch app.connection {
        case .online: return app.workspaceName
        case .connecting: return "подключение"
        case .failed: return "нет связи"
        case .disconnected: return "подключить"
        }
    }

    var body: some View {
        Button {
            if let action {
                action()
            } else {
                app.selectedTab = .settings
            }
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
                    .shadow(color: color.opacity(0.6), radius: 5)
                Text(text)
                    .font(RbitFont.mono(11, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                    .lineLimit(1)
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Color.rbitFaint)
            }
            .padding(.horizontal, 10)
            .frame(height: 28)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.rbitSurface)
            )
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )
        }
        .buttonStyle(PressableButtonStyle())
    }
}

struct AvatarView: View {
    let name: String
    var size: CGFloat = 34
    var showStatus: Bool = false
    var statusColor: Color = .rbitGreen

    private var initials: String {
        RbitFormat.initials(name)
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            ZStack {
                Circle()
                    .fill(Color.rbitPanel)
                Circle()
                    .strokeBorder(
                        AngularGradient(
                            colors: [
                                Color.rbitAccent,
                                Color.rbitViolet,
                                Color.rbitBlue,
                                Color.rbitAccent,
                            ],
                            center: .center
                        ),
                        lineWidth: max(1, size * 0.045)
                    )
                Text(initials)
                    .font(.system(
                        size: size * 0.32,
                        weight: .bold,
                        design: .monospaced
                    ))
                    .foregroundStyle(Color.rbitText)
            }
            .frame(width: size, height: size)

            if showStatus {
                Circle()
                    .fill(statusColor)
                    .frame(width: size * 0.24, height: size * 0.24)
                    .overlay(
                        Circle().strokeBorder(Color.rbitBackground, lineWidth: max(1.5, size * 0.045))
                    )
                    .offset(x: -size * 0.02, y: -size * 0.02)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(name)
    }
}

struct SectionLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(RbitFont.mono(10, weight: .semibold))
            .tracking(1.4)
            .foregroundStyle(Color.rbitDim)
    }
}

struct IconButton: View {
    let icon: String
    var size: CGFloat = 36
    var tint: Color = .rbitText
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: size * 0.38, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: size, height: size)
                .background(
                    Circle()
                        .fill(Color.rbitSurface)
                )
                .overlay(
                    Circle()
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )
        }
        .buttonStyle(PressableButtonStyle())
    }
}

struct ToastView: View {
    let toast: Toast

    private var icon: String {
        switch toast.kind {
        case .info: return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .error: return "exclamationmark.triangle.fill"
        }
    }

    private var color: Color {
        switch toast.kind {
        case .info: return .rbitBlue
        case .success: return .rbitGreen
        case .error: return .rbitRed
        }
    }

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: icon)
                .foregroundStyle(color)
            Text(toast.message)
                .font(RbitFont.ui(13, weight: .medium))
                .foregroundStyle(Color.rbitText)
                .multilineTextAlignment(.leading)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(
            Capsule(style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .background(
            Capsule(style: .continuous)
                .fill(Color.rbitGlass)
        )
        .overlay(
            Capsule(style: .continuous)
                .strokeBorder(color.opacity(0.35), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.24), radius: 20, y: 8)
        .padding(.horizontal, 20)
    }
}

struct CopyButton: View {
    let value: String
    var title: String = "Копировать"
    var compact: Bool = false

    @State private var copied = false
    @State private var resetTask: Task<Void, Never>?

    var body: some View {
        Button {
            UIPasteboard.general.string = value
            Haptics.tap()
            copied = true
            resetTask?.cancel()
            resetTask = Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard !Task.isCancelled else { return }
                copied = false
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: compact ? 10 : 12, weight: .semibold))
                if !compact {
                    Text(copied ? "Скопировано" : title)
                        .font(RbitFont.mono(11, weight: .medium))
                }
            }
            .foregroundStyle(copied ? Color.rbitGreen : Color.rbitDim)
            .padding(.horizontal, compact ? 8 : 10)
            .frame(height: compact ? 24 : 28)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.rbitPanel)
            )
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )
        }
        .buttonStyle(PressableButtonStyle())
    }
}

struct CommandBlock: View {
    let command: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(command)
                .font(RbitFont.mono(11))
                .foregroundStyle(Color.rbitText)
                .lineLimit(4)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack {
                Spacer()
                CopyButton(value: command, title: "Копировать команду", compact: true)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.rbitCodeBackground)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.rbitBorder, lineWidth: 1)
        )
    }
}

struct PickerRow: View {
    let title: String
    var subtitle: String?
    var icon: String?
    var tint: Color = .rbitAccent
    var selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                if let icon {
                    Image(systemName: icon)
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(tint)
                        .frame(width: 34, height: 34)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(tint.opacity(0.12))
                        )
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(RbitFont.ui(14, weight: .semibold))
                        .foregroundStyle(Color.rbitText)
                        .multilineTextAlignment(.leading)
                    if let subtitle, !subtitle.isEmpty {
                        Text(subtitle)
                            .font(RbitFont.ui(11))
                            .foregroundStyle(Color.rbitDim)
                            .multilineTextAlignment(.leading)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.rbitAccent)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(selected ? Color.rbitAccent.opacity(0.07) : Color.clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(
                        selected ? Color.rbitAccent.opacity(0.28) : Color.clear,
                        lineWidth: 1
                    )
            )
        }
        .buttonStyle(PressableButtonStyle())
    }
}

struct SheetHeader: View {
    let title: String
    var subtitle: String?
    let onClose: () -> Void

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(RbitFont.ui(22, weight: .bold))
                    .foregroundStyle(Color.rbitText)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(RbitFont.ui(12))
                        .foregroundStyle(Color.rbitDim)
                }
            }
            Spacer()
            IconButton(icon: "xmark", size: 32, action: onClose)
        }
    }
}

struct EffortBars: View {
    let bars: Int
    var tint: Color = .rbitAccent

    var body: some View {
        HStack(alignment: .bottom, spacing: 2) {
            ForEach(0..<3, id: \.self) { index in
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(index < bars ? tint : Color.rbitFaint.opacity(0.35))
                    .frame(width: 3, height: CGFloat(5 + index * 3))
            }
        }
        .frame(width: 14, height: 12, alignment: .bottom)
    }
}

struct ComposerView: View {
    @EnvironmentObject private var app: AppState

    @Binding var text: String
    var sessionID: String?
    var placeholder: String = "Спросите что угодно…"
    var compact: Bool = false
    var onSend: () -> Void
    var onStop: () -> Void

    @FocusState private var focused: Bool
    @State private var showModelPicker = false
    @State private var showEffortPicker = false
    @State private var showAgentPicker = false

    private var ownTurn: Bool {
        guard let sessionID else { return false }
        return app.activeTurn?.sessionID == sessionID
    }

    private var busyElsewhere: Bool {
        app.isStreaming && !ownTurn
    }

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !app.isStartingTurn
            && !busyElsewhere
            && app.connection.isOnline
    }

    private var agentTint: Color {
        app.settings.agent == "plan" ? .rbitBlue : .rbitAccent
    }

    var body: some View {
        VStack(spacing: 10) {
            TextField(placeholder, text: $text, axis: .vertical)
                .font(RbitFont.ui(compact ? 15 : 16))
                .foregroundStyle(Color.rbitText)
                .tint(Color.rbitAccent)
                .lineLimit(compact ? 1...5 : 2...7)
                .focused($focused)
                .submitLabel(.return)
                .padding(.horizontal, 2)
                .frame(minHeight: compact ? 24 : 54, alignment: .topLeading)
                .disabled(ownTurn)

            HStack(spacing: 6) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        chip(
                            icon: app.settings.agent == "plan" ? "doc.text.magnifyingglass" : "hammer.fill",
                            title: app.selectedAgent.displayTitle,
                            tint: agentTint
                        ) {
                            showAgentPicker = true
                        }

                        chip(icon: "cpu", title: app.modelChipTitle) {
                            showModelPicker = true
                        }

                        chip(
                            icon: app.settings.effort == "auto" ? "sparkles" : "speedometer",
                            title: app.selectedEffort.title.lowercased()
                        ) {
                            showEffortPicker = true
                        }
                    }
                }
                .frame(maxWidth: 220, alignment: .leading)

                Spacer(minLength: 4)

                if app.isStartingTurn {
                    ProgressView()
                        .tint(Color.rbitAccent)
                        .frame(width: 36, height: 36)
                } else if ownTurn {
                    Button(action: onStop) {
                        ZStack {
                            Circle()
                                .fill(Color.rbitPanelRaised)
                            if app.isStopping {
                                ProgressView()
                                    .tint(Color.rbitText)
                                    .scaleEffect(0.7)
                            } else {
                                RoundedRectangle(cornerRadius: 3, style: .continuous)
                                    .fill(Color.rbitRed)
                                    .frame(width: 12, height: 12)
                            }
                        }
                        .frame(width: 36, height: 36)
                        .overlay(
                            Circle().strokeBorder(Color.rbitBorder, lineWidth: 1)
                        )
                    }
                    .buttonStyle(PressableButtonStyle())
                    .accessibilityLabel("Остановить")
                } else {
                    Button {
                        guard canSend else { return }
                        focused = false
                        onSend()
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(Color(hex: 0x141414))
                            .frame(width: 36, height: 36)
                            .background(
                                Circle()
                                    .fill(canSend ? Color.rbitAccent : Color.rbitPanelRaised)
                            )
                            .opacity(canSend ? 1 : 0.55)
                    }
                    .buttonStyle(PressableButtonStyle())
                    .disabled(!canSend)
                    .accessibilityLabel("Отправить")
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 10)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color.rbitSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(
                    focused ? Color.rbitAccent.opacity(0.38) : Color.rbitBorder,
                    lineWidth: 1
                )
        )
        .shadow(color: Color.black.opacity(0.18), radius: 18, y: 8)
        .sheet(isPresented: $showModelPicker) {
            ModelPickerSheet()
                .environmentObject(app)
        }
        .sheet(isPresented: $showEffortPicker) {
            EffortPickerSheet()
                .environmentObject(app)
        }
        .sheet(isPresented: $showAgentPicker) {
            AgentPickerSheet()
                .environmentObject(app)
        }
    }

    private func chip(
        icon: String,
        title: String,
        tint: Color = .rbitDim,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(tint)
                Text(title)
                    .font(RbitFont.mono(10, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                    .lineLimit(1)
                Image(systemName: "chevron.down")
                    .font(.system(size: 6, weight: .bold))
                    .foregroundStyle(Color.rbitFaint)
            }
            .padding(.horizontal, 8)
            .frame(height: 26)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.rbitPanel)
            )
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )
        }
        .buttonStyle(PressableButtonStyle())
    }
}
