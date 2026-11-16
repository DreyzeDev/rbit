import SwiftUI

struct ToolCallCard: View {
    let name: String
    var arguments: [String: JSONValue]?
    var status: String
    var summary: String?
    var output: String?
    var diff: String?
    var durationMS: Int?

    @State private var expanded = false

    private var isError: Bool {
        status == "error" || status == "denied"
    }

    private var isRunning: Bool {
        status == "running" || status == "pending"
    }

    private var tint: Color {
        if isError { return .rbitRed }
        if isRunning { return .rbitAccent }
        return .rbitGreen
    }

    private var icon: String {
        switch name.lowercased() {
        case "bash", "shell", "terminal":
            return "terminal"
        case "read", "grep", "glob", "symbols":
            return "doc.text.magnifyingglass"
        case "write", "edit", "multi_edit", "apply_patch":
            return "square.and.pencil"
        case "webfetch", "websearch":
            return "globe"
        case "todowrite":
            return "checklist"
        case "git":
            return "arrow.triangle.branch"
        case "diagnostics", "lint":
            return "stethoscope"
        default:
            return "wrench.and.screwdriver"
        }
    }

    private var statusText: String {
        switch status {
        case "running": return "выполняется"
        case "pending": return "ожидание"
        case "success", "completed": return "готово"
        case "error": return "ошибка"
        case "denied": return "запрещено"
        default: return status
        }
    }

    private var summaryText: String? {
        if let summary, !summary.isEmpty { return summary }
        if let arguments, let value = arguments.toolSummary, !value.isEmpty {
            return value
        }
        return nil
    }

    private var durationText: String? {
        guard let durationMS, durationMS > 0 else { return nil }
        return String(format: "%.1f с", Double(durationMS) / 1000)
    }

    private var displayedOutput: String {
        guard let output, !output.isEmpty else { return "" }
        if output.count > 3_000 {
            return String(output.prefix(3_000)) + "\n… [откройте на ПК для полного вывода]"
        }
        return output
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeOut(duration: 0.18)) {
                    expanded.toggle()
                }
                Haptics.tap()
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: icon)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(tint)
                        .frame(width: 30, height: 30)
                        .background(
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .fill(tint.opacity(0.11))
                        )

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(name)
                                .font(RbitFont.mono(11, weight: .semibold))
                                .foregroundStyle(Color.rbitText)
                            Text(statusText)
                                .font(RbitFont.mono(8, weight: .medium))
                                .foregroundStyle(tint)
                                .padding(.horizontal, 5)
                                .frame(height: 15)
                                .background(
                                    Capsule(style: .continuous)
                                        .fill(tint.opacity(0.1))
                                )
                        }
                        if let summaryText {
                            Text(summaryText)
                                .font(RbitFont.mono(9))
                                .foregroundStyle(Color.rbitDim)
                                .lineLimit(1)
                        }
                    }

                    Spacer(minLength: 4)

                    if isRunning {
                        ProgressView()
                            .tint(Color.rbitAccent)
                            .scaleEffect(0.64)
                    } else if let durationText {
                        Text(durationText)
                            .font(RbitFont.mono(8))
                            .foregroundStyle(Color.rbitFaint)
                    }

                    Image(systemName: "chevron.down")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(Color.rbitFaint)
                        .rotationEffect(.degrees(expanded ? 180 : 0))
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                RbitDivider()
                    .padding(.horizontal, 10)

                VStack(alignment: .leading, spacing: 12) {
                    if let arguments, !arguments.isEmpty {
                        detailSection(title: "Аргументы") {
                            Text(arguments.pretty)
                                .font(RbitFont.mono(9.5))
                                .foregroundStyle(Color.rbitDim)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }

                    if !displayedOutput.isEmpty {
                        detailSection(title: isError ? "Ошибка" : "Результат") {
                            Text(displayedOutput)
                                .font(RbitFont.mono(9.5))
                                .foregroundStyle(isError ? Color.rbitRed : Color.rbitDim)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            if let output, output.count > 3_000 {
                                CopyButton(value: output, title: "Копировать полностью", compact: true)
                                    .padding(.top, 4)
                            }
                        }
                    }

                    if let diff, !diff.isEmpty {
                        detailSection(title: "Diff") {
                            CodeBlockView(language: "diff", code: diff)
                        }
                    }
                }
                .padding(10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .fill(Color.rbitPanel.opacity(0.46))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .strokeBorder(
                    isError ? Color.rbitRed.opacity(0.28) : Color.rbitBorder,
                    lineWidth: 1
                )
        )
    }

    private func detailSection<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title.uppercased())
                .font(RbitFont.mono(8, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Color.rbitFaint)
            content()
        }
    }
}
