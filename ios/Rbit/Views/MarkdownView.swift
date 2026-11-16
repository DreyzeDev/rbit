import Foundation
import SwiftUI
import UIKit

enum MarkdownBlock {
    case paragraph(String)
    case heading(Int, String)
    case bullet(String)
    case numbered(String, String)
    case quote(String)
    case code(language: String?, text: String)
    case divider
    case spacer
}

enum MarkdownParser {
    static func parse(_ text: String) -> [MarkdownBlock] {
        let normalized = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var codeLines: [String] = []
        var codeLanguage: String?
        var inCode = false

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            blocks.append(.paragraph(paragraph.joined(separator: "\n")))
            paragraph.removeAll()
        }

        for rawLine in normalized.components(separatedBy: "\n") {
            let trimmed = rawLine.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                if inCode {
                    blocks.append(.code(language: codeLanguage, text: codeLines.joined(separator: "\n")))
                    codeLines.removeAll()
                    codeLanguage = nil
                    inCode = false
                } else {
                    flushParagraph()
                    inCode = true
                    let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                    codeLanguage = language.isEmpty ? nil : language
                }
                continue
            }

            if inCode {
                codeLines.append(rawLine)
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                continue
            }

            if trimmed == "---" || trimmed == "***" || trimmed == "___" {
                flushParagraph()
                blocks.append(.divider)
                continue
            }

            let hashes = rawLine.prefix(while: { $0 == "#" }).count
            if (1...6).contains(hashes) {
                let rest = rawLine.dropFirst(hashes)
                if rest.first == " " {
                    flushParagraph()
                    blocks.append(
                        .heading(hashes, String(rest.dropFirst()).trimmingCharacters(in: .whitespaces))
                    )
                    continue
                }
            }

            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
                flushParagraph()
                blocks.append(.bullet(String(trimmed.dropFirst(2))))
                continue
            }

            if trimmed.hasPrefix("> ") {
                flushParagraph()
                blocks.append(.quote(String(trimmed.dropFirst(2))))
                continue
            }

            let digits = trimmed.prefix(while: { $0.isNumber })
            if !digits.isEmpty {
                let rest = trimmed.dropFirst(digits.count)
                if let separator = rest.first, separator == "." || separator == ")" {
                    let body = rest.dropFirst()
                    if body.first == " " {
                        flushParagraph()
                        blocks.append(
                            .numbered(String(digits), String(body.dropFirst()))
                        )
                        continue
                    }
                }
            }

            paragraph.append(rawLine)
        }

        if inCode {
            blocks.append(.code(language: codeLanguage, text: codeLines.joined(separator: "\n")))
        }
        flushParagraph()
        return blocks
    }
}

struct MarkdownView: View {
    let text: String

    var body: some View {
        let blocks = MarkdownParser.parse(text)
        VStack(alignment: .leading, spacing: 8) {
            ForEach(blocks.indices, id: \.self) { index in
                blockView(blocks[index])
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func blockView(_ block: MarkdownBlock) -> some View {
        switch block {
        case .paragraph(let value):
            InlineMarkdownText(text: value, size: 14)
                .lineSpacing(3)
        case .heading(let level, let value):
            InlineMarkdownText(
                text: value,
                size: level <= 2 ? 19 : 16,
                weight: .bold
            )
            .padding(.top, level <= 2 ? 4 : 2)
        case .bullet(let value):
            HStack(alignment: .top, spacing: 8) {
                Text("•")
                    .font(RbitFont.mono(13, weight: .bold))
                    .foregroundStyle(Color.rbitAccent)
                InlineMarkdownText(text: value, size: 14)
            }
        case .numbered(let number, let value):
            HStack(alignment: .top, spacing: 8) {
                Text("\(number).")
                    .font(RbitFont.mono(11, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                    .frame(minWidth: 18, alignment: .trailing)
                InlineMarkdownText(text: value, size: 14)
            }
        case .quote(let value):
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(Color.rbitAccent.opacity(0.5))
                    .frame(width: 2)
                InlineMarkdownText(text: value, size: 13)
                    .foregroundStyle(Color.rbitDim)
            }
        case .code(let language, let value):
            CodeBlockView(language: language, code: value)
        case .divider:
            RbitDivider()
                .padding(.vertical, 2)
        case .spacer:
            Color.clear.frame(height: 2)
        }
    }
}

struct InlineMarkdownText: View {
    let text: String
    var size: CGFloat = 14
    var weight: Font.Weight = .regular

    private var attributed: AttributedString {
        if let value = try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return value
        }
        return AttributedString(text)
    }

    var body: some View {
        Text(attributed)
            .font(RbitFont.ui(size, weight: weight))
            .foregroundStyle(Color.rbitText)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct CodeBlockView: View {
    let language: String?
    let code: String

    private var displayed: String {
        if code.count > 12_000 {
            return String(code.prefix(12_000)) + "\n… [обрезано]"
        }
        return code
    }

    private var isDiff: Bool {
        let value = (language ?? "").lowercased()
        return value == "diff" || value == "patch"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text((language?.isEmpty == false ? language! : "code").lowercased())
                    .font(RbitFont.mono(9, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                Spacer()
                CopyButton(value: code, compact: true)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)

            RbitDivider()

            if isDiff {
                ScrollView(.horizontal, showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(displayed.components(separatedBy: "\n").indices), id: \.self) { index in
                            let line = displayed.components(separatedBy: "\n")[index]
                            Text(line.isEmpty ? " " : line)
                                .font(RbitFont.mono(10.5))
                                .foregroundStyle(diffColor(for: line))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 1)
                                .background(diffBackground(for: line))
                                .fixedSize(horizontal: true, vertical: false)
                        }
                    }
                    .padding(.vertical, 6)
                }
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(displayed)
                        .font(RbitFont.mono(10.5))
                        .foregroundStyle(Color.rbitText)
                        .lineSpacing(2)
                        .padding(10)
                        .fixedSize(horizontal: true, vertical: false)
                        .textSelection(.enabled)
                }
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.rbitCodeBackground)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.rbitBorder, lineWidth: 1)
        )
    }

    private func diffColor(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return .rbitGreen }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return .rbitRed }
        if line.hasPrefix("@@") { return .rbitAccent }
        return .rbitDim
    }

    private func diffBackground(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return .rbitGreen.opacity(0.08) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return .rbitRed.opacity(0.08) }
        return .clear
    }
}
