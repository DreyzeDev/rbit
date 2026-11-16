import Foundation
import SwiftUI

enum AppTheme: String, Codable, CaseIterable, Identifiable, Hashable {
    case dark
    case light
    case system

    var id: String { rawValue }

    var title: String {
        switch self {
        case .dark: return "rbit Dark"
        case .light: return "rbit Light"
        case .system: return "Системная"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .dark: return .dark
        case .light: return .light
        case .system: return nil
        }
    }
}

struct AppSettings: Codable, Equatable {
    var serverURL: String = ""
    var modelID: String = ""
    var effort: String = "auto"
    var agent: String = "build"
    var displayName: String = "rbit user"
    var theme: AppTheme = .dark
    var hasConnectedOnce: Bool = false
    var lastSessionID: String?

    static let storageKey = "rbit.settings.v1"

    static func load() -> AppSettings {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let value = try? JSONDecoder().decode(AppSettings.self, from: data) else {
            return AppSettings()
        }
        return value
    }

    func save() {
        guard let data = try? JSONEncoder().encode(self) else { return }
        UserDefaults.standard.set(data, forKey: Self.storageKey)
    }
}

enum RbitRemoteDefaults {
    static let serverURL = "https://31.77.11.16/rbit"
}

struct EffortOption: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let bars: Int

    static let all: [EffortOption] = [
        EffortOption(id: "auto", title: "Авто", subtitle: "rbit выберет сам", bars: 0),
        EffortOption(id: "low", title: "Низкий", subtitle: "Быстрее и короче", bars: 1),
        EffortOption(id: "medium", title: "Средний", subtitle: "Баланс скорости и глубины", bars: 2),
        EffortOption(id: "high", title: "Высокий", subtitle: "Больше размышлений", bars: 3),
    ]

    static func option(for id: String) -> EffortOption? {
        all.first(where: { $0.id == id })
    }
}
