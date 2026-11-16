import Foundation

enum AppTab: String, CaseIterable, Identifiable, Hashable {
    case new
    case history
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .new: return "Новый"
        case .history: return "История"
        case .settings: return "Настройки"
        }
    }

    var icon: String {
        switch self {
        case .new: return "plus"
        case .history: return "clock.arrow.circlepath"
        case .settings: return "person.crop.circle"
        }
    }
}

enum ConnectionState: Equatable {
    case disconnected
    case connecting
    case online
    case failed(String)

    var isOnline: Bool {
        if case .online = self { return true }
        return false
    }

    var label: String {
        switch self {
        case .disconnected: return "не подключено"
        case .connecting: return "подключение"
        case .online: return "на связи"
        case .failed: return "нет связи"
        }
    }
}

struct WorkspaceInfo: Decodable, Equatable {
    let path: String?
    let name: String?
}

struct AgentInfo: Decodable, Identifiable, Equatable {
    let name: String
    let title: String?
    let description: String?

    var id: String { name }
    var displayTitle: String { title ?? name.capitalized }
    var displayDescription: String { description ?? "" }

    static let defaults: [AgentInfo] = [
        AgentInfo(name: "build", title: "Build", description: "Пишет код и запускает инструменты"),
        AgentInfo(name: "plan", title: "Plan", description: "Только читает и планирует"),
    ]
}

struct ServerStatus: Decodable, Equatable {
    let ok: Bool?
    let app: String?
    let version: String?
    let api: Int?
    let workspace: WorkspaceInfo?
    let model: String?
    let effort: String?
    let efforts: [String]?
    let agents: [AgentInfo]?
    let approvals: Bool?
    let auth: Bool?
    let busy: [String]?
}

struct HealthResponse: Decodable {
    let status: String?
    let ok: Bool?
    let app: String?
    let version: String?
    let api: Int?
    let workspace: String?
    let name: String?
}

struct ModelInfo: Decodable, Identifiable {
    let id: String
    let name: String
    let provider: String
    let contextWindow: Int?
    let costPer1K: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case provider
        case contextWindow = "context_window"
        case costPer1K = "cost_per_1k"
    }

    var displayName: String { name.isEmpty ? id : name }
    var displayProvider: String { provider.isEmpty ? "rbit CLI" : provider }
    var contextLabel: String? {
        guard let contextWindow, contextWindow > 0 else { return nil }
        if contextWindow >= 1_000_000 {
            return String(format: "%.1fM контекст", Double(contextWindow) / 1_000_000)
        }
        if contextWindow >= 1_000 {
            return "\(contextWindow / 1_000)k контекст"
        }
        return "\(contextWindow) контекст"
    }
}

struct ModelsResponse: Decodable {
    let models: [ModelInfo]
    let current: String?
    let effort: String?
    let efforts: [String]?
    let workspace: String?
}

struct SessionSummary: Identifiable, Decodable {
    let id: String
    let title: String?
    let createdAt: String?
    let updatedAt: String?
    let messages: Int?
    let finished: Bool?
    let archived: Bool?
    let tokens: Int?
    let preview: String?
    let model: String?
    let agent: String?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case messages
        case finished
        case archived
        case tokens
        case preview
        case model
        case agent
    }

    var displayTitle: String {
        let value = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? "Новая сессия" : value
    }

    var displayPreview: String {
        let value = preview?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value
    }

    var isRunning: Bool { finished == false }
    var messageCount: Int { messages ?? 0 }

    var date: Date? {
        RbitFormat.parseDate(updatedAt) ?? RbitFormat.parseDate(createdAt)
    }
}

struct SessionDetail: Decodable {
    var id: String
    var title: String?
    var createdAt: String?
    var updatedAt: String?
    var finished: Bool?
    var archived: Bool?
    var tokens: Int?
    var model: String?
    var agent: String?
    var messageCount: Int?
    var messages: [ChatMessage]?
    var todos: [TodoItem]?
    var modifiedFiles: [String: ModifiedFile]?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case finished
        case archived
        case tokens
        case model
        case agent
        case messageCount = "message_count"
        case messages
        case todos
        case modifiedFiles = "modified_files"
    }

    var displayTitle: String {
        let value = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? "Новая сессия" : value
    }

    var messageList: [ChatMessage] { messages ?? [] }
    var totalTokens: Int { tokens ?? 0 }
}

struct ChatMessage: Identifiable, Decodable {
    let id: String
    let role: String
    let content: String
    let agent: String?
    let model: String?
    let timestamp: String?
    let tokens: Int?
    let durationS: Double?
    let attachments: [String]?
    let toolCalls: [ToolCallRecord]?

    enum CodingKeys: String, CodingKey {
        case id
        case role
        case content
        case agent
        case model
        case timestamp
        case tokens
        case durationS = "duration_s"
        case attachments
        case toolCalls = "tool_calls"
    }

    init(
        id: String = UUID().uuidString,
        role: String,
        content: String,
        agent: String? = nil,
        model: String? = nil,
        timestamp: String? = nil,
        tokens: Int? = nil,
        durationS: Double? = nil,
        attachments: [String]? = nil,
        toolCalls: [ToolCallRecord]? = nil
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.agent = agent
        self.model = model
        self.timestamp = timestamp
        self.tokens = tokens
        self.durationS = durationS
        self.attachments = attachments
        self.toolCalls = toolCalls
    }

    var isUser: Bool { role == "user" }
    var isAssistant: Bool { role == "assistant" }
    var modelLabel: String { model ?? "" }
}

struct ToolCallRecord: Identifiable, Decodable {
    let id: String
    let name: String
    let arguments: [String: JSONValue]?
    let status: String
    let summary: String?
    let output: String?
    let diff: String?
    let durationMS: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case arguments
        case status
        case summary
        case output
        case diff
        case durationMS = "duration_ms"
    }

    var isError: Bool { status == "error" || status == "denied" }
    var isRunning: Bool { status == "running" || status == "pending" }
}

struct TodoItem: Identifiable, Decodable {
    let id: String
    let content: String
    let status: String
    let priority: String
}

struct ModifiedFile: Decodable {
    let added: Int?
    let removed: Int?
}

struct StreamEvent: Decodable {
    let type: String
    let text: String?
    let content: String?
    let sessionID: String?
    let title: String?
    let createdAt: String?
    let id: String?
    let name: String?
    let arguments: [String: JSONValue]?
    let status: String?
    let summary: String?
    let output: String?
    let diff: String?
    let durationMS: Int?
    let active: Bool?
    let tool: String?
    let input: String?
    let destructive: Bool?
    let message: ChatMessage?

    enum CodingKeys: String, CodingKey {
        case type
        case text
        case content
        case sessionID = "session_id"
        case title
        case createdAt = "created_at"
        case id
        case name
        case arguments
        case status
        case summary
        case output
        case diff
        case durationMS = "duration_ms"
        case active
        case tool
        case input
        case destructive
        case message
    }
}

indirect enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
            return
        }
        if let value = try? container.decode(Double.self) {
            self = .number(value)
            return
        }
        if let value = try? container.decode(String.self) {
            self = .string(value)
            return
        }
        if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
            return
        }
        if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
            return
        }
        throw DecodingError.dataCorruptedError(
            in: container,
            debugDescription: "Unsupported JSON value"
        )
    }

    var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    var compact: String {
        switch self {
        case .string(let value): return value
        case .number(let value):
            if value.rounded() == value {
                return String(Int(value))
            }
            return String(format: "%g", value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return "null"
        case .array, .object: return pretty
        }
    }

    var pretty: String {
        render(indent: 0)
    }

    private func render(indent: Int) -> String {
        let pad = String(repeating: "  ", count: indent)
        let next = String(repeating: "  ", count: indent + 1)
        switch self {
        case .string(let value):
            let escaped = value
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
                .replacingOccurrences(of: "\t", with: "\\t")
            return "\"\(escaped)\""
        case .number(let value):
            if value.rounded() == value {
                return String(Int(value))
            }
            return String(format: "%g", value)
        case .bool(let value):
            return value ? "true" : "false"
        case .null:
            return "null"
        case .array(let values):
            guard !values.isEmpty else { return "[]" }
            let body = values.map { next + $0.render(indent: indent + 1) }.joined(separator: ",\n")
            return "[\n\(body)\n\(pad)]"
        case .object(let dictionary):
            guard !dictionary.isEmpty else { return "{}" }
            let body = dictionary.keys.sorted().map { key in
                let escapedKey = key
                    .replacingOccurrences(of: "\\", with: "\\\\")
                    .replacingOccurrences(of: "\"", with: "\\\"")
                return "\(next)\"\(escapedKey)\": \(dictionary[key]!.render(indent: indent + 1))"
            }.joined(separator: ",\n")
            return "{\n\(body)\n\(pad)}"
        }
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    var pretty: String {
        JSONValue.object(self).pretty
    }

    var toolSummary: String? {
        for key in ["command", "filePath", "path", "pattern", "query", "description", "url", "prompt"] {
            if let value = self[key]?.stringValue, !value.isEmpty {
                return value
            }
        }
        if let first = self.keys.sorted().first, let value = self[first] {
            return "\(first): \(value.compact)"
        }
        return nil
    }
}

struct LiveTool: Identifiable {
    var id: String
    var name: String
    var arguments: [String: JSONValue]?
    var status: String
    var summary: String?
    var output: String?
    var diff: String?
    var durationMS: Int?
    var startedAt: Date = Date()

    var isError: Bool { status == "error" || status == "denied" }
    var isRunning: Bool { status == "running" || status == "pending" }
}

struct ActiveTurn {
    let sessionID: String
    let prompt: String
    var assistantText: String = ""
    var reasoningText: String = ""
    var statusText: String = ""
    var thinking: Bool = false
    var tools: [LiveTool] = []
    let startedAt: Date = Date()
    var error: String? = nil
    var aborted: Bool = false
}

struct PendingApproval: Identifiable, Equatable {
    let id: String
    let tool: String
    let input: String
    let destructive: Bool
}

struct SessionRoute: Identifiable {
    let id: String
    var title: String

    init(id: String, title: String) {
        self.id = id
        self.title = title
    }
}

struct ApiAck: Decodable {
    let ok: Bool?
    let deleted: Bool?
    let aborted: Bool?
    let id: String?
    let title: String?
}

struct Toast: Identifiable, Equatable {
    enum Kind: String, Equatable {
        case info
        case success
        case error
    }

    let id = UUID()
    let message: String
    let kind: Kind
}

struct PairingLink: Equatable {
    let url: String
    let token: String?

    init?(url: URL) {
        guard url.scheme?.lowercased() == "rbit" else { return nil }
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first(where: { $0.name.lowercased() == name })?.value
        }
        guard let rawURL = value("url"), !rawURL.isEmpty else { return nil }
        self.url = rawURL
        self.token = value("token") ?? value("pair")
    }

    init?(raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if let url = URL(string: text), url.scheme?.lowercased() == "rbit" {
            self.init(url: url)
            return
        }
        guard let components = URLComponents(string: text) else { return nil }
        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first(where: { $0.name.lowercased() == name })?.value
        }
        guard let url = value("url"), !url.isEmpty else { return nil }
        self.url = url
        self.token = value("token") ?? value("pair")
    }
}
