import Foundation

enum RbitFormat {
    private static let isoWithFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let legacyFormats = [
        "yyyy-MM-dd'T'HH:mm:ss",
        "yyyy-MM-dd'T'HH:mm:ss.SSS",
        "yyyy-MM-dd HH:mm:ss",
        "yyyy-MM-dd HH:mm",
        "dd.MM.yyyy HH:mm",
    ]

    static func parseDate(_ raw: String?) -> Date? {
        guard let raw, !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let date = isoWithFraction.date(from: value) ?? iso.date(from: value) {
            return date
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        for format in legacyFormats {
            formatter.dateFormat = format
            if let date = formatter.date(from: value) {
                return date
            }
        }
        return nil
    }

    static func relative(_ raw: String?) -> String {
        guard let date = parseDate(raw) else { return "" }
        let seconds = Date().timeIntervalSince(date)
        if seconds < 60 {
            return "только что"
        }
        if seconds < 3600 {
            return "\(Int(seconds / 60)) мин назад"
        }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            return time(date)
        }
        if calendar.isDateInYesterday(date) {
            return "вчера"
        }
        if let days = calendar.dateComponents([.day], from: date, to: Date()).day, days < 7, days > 1 {
            return "\(days) дн назад"
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateFormat = calendar.component(.year, from: date) == calendar.component(.year, from: Date())
            ? "d MMM"
            : "dd.MM.yy"
        return formatter.string(from: date)
    }

    static func time(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    static func groupTitle(for raw: String?) -> String {
        guard let date = parseDate(raw) else { return "Ранее" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            return "Сегодня"
        }
        if calendar.isDateInYesterday(date) {
            return "Вчера"
        }
        let days = calendar.dateComponents([.day], from: date, to: Date()).day ?? 0
        if days < 7 {
            return "На этой неделе"
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateFormat = calendar.component(.year, from: date) == calendar.component(.year, from: Date())
            ? "LLLL"
            : "LLLL yyyy"
        return formatter.string(from: date).capitalized
    }

    static func number(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.numberStyle = .decimal
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    static func tokens(_ value: Int) -> String {
        if value >= 1000 {
            return String(format: "%.1fk", Double(value) / 1000)
        }
        return "\(value)"
    }

    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds > 0 else { return "" }
        if seconds < 60 {
            return String(format: "%.1f с", seconds)
        }
        let total = Int(seconds.rounded())
        return "\(total / 60) мин \(total % 60) с"
    }

    static func initials(_ name: String) -> String {
        let parts = name
            .split(whereSeparator: { $0 == " " || $0 == "\t" || $0 == "\n" })
            .prefix(2)
        let letters = parts.compactMap { $0.first }.map(String.init).joined()
        let value = letters.isEmpty ? "r" : letters.uppercased()
        return String(value.prefix(2))
    }

    static func shortModel(_ id: String?) -> String {
        guard let id, !id.isEmpty else { return "rbit CLI" }
        var value = id
        if let hash = value.firstIndex(of: "#") {
            value = String(value[..<hash])
        }
        if let slash = value.lastIndex(of: "/") {
            value = String(value[value.index(after: slash)...])
        }
        return value
    }

    static func truncate(_ value: String, limit: Int) -> String {
        guard value.count > limit else { return value }
        return String(value.prefix(limit)).trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }

    static func nonEmpty(_ value: String) -> String? {
        value.isEmpty ? nil : value
    }

    static func chatCount(_ count: Int) -> String {
        let mod100 = count % 100
        let mod10 = count % 10
        if mod100 >= 11 && mod100 <= 14 { return "\(count) чатов" }
        if mod10 == 1 { return "\(count) чат" }
        if (2...4).contains(mod10) { return "\(count) чата" }
        return "\(count) чатов"
    }

    static func readableError(_ error: Error) -> String {
        if let localized = error as? LocalizedError, let description = localized.errorDescription {
            return description
        }
        return error.localizedDescription
    }
}
