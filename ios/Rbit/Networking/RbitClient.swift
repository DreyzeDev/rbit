import Foundation

enum RbitClientError: LocalizedError, Equatable {
    case invalidURL
    case unauthorized
    case http(Int, String)
    case transport(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Некорректный адрес сервера rbit."
        case .unauthorized:
            return "Неверный токен. Скопируйте новый токен из rbit mobile."
        case .http(let status, let message):
            if message.isEmpty {
                return "Сервер rbit ответил ошибкой \(status)."
            }
            return message
        case .transport(let message):
            return message
        case .cancelled:
            return "Запрос отменён."
        }
    }

    var isCancellation: Bool {
        if case .cancelled = self { return true }
        return false
    }
}

struct RbitClient {
    let baseURL: URL
    let token: String?

    private let session: URLSession

    init(baseURL: URL, token: String?) {
        var value = baseURL.absoluteString
        while value.hasSuffix("/") {
            value.removeLast()
        }
        self.baseURL = URL(string: value) ?? baseURL
        self.token = token?.trimmingCharacters(in: .whitespacesAndNewlines)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 3600
        configuration.waitsForConnectivity = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: configuration)
    }

    static func parseBaseURL(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        if text.lowercased().hasPrefix("rbit://") {
            guard let link = PairingLink(raw: text) else { return nil }
            text = link.url
        }

        if !text.contains("://") {
            text = "http://" + text
        }
        guard var components = URLComponents(string: text) else { return nil }
        guard let scheme = components.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return nil
        }
        guard let host = components.host, !host.isEmpty else { return nil }
        if components.port == nil, scheme == "http" {
            components.port = 8333
        }
        if components.path == "/" {
            components.path = ""
        }
        while components.path.hasSuffix("/") {
            components.path = String(components.path.dropLast())
        }
        components.query = nil
        components.fragment = nil
        return components.url
    }

    func health() async throws -> HealthResponse {
        try await get("/health")
    }

    func status() async throws -> ServerStatus {
        try await get("/v1/status")
    }

    func models() async throws -> ModelsResponse {
        try await get("/v1/models")
    }

    func sessions() async throws -> [SessionSummary] {
        let response: SessionsResponse = try await get("/v1/sessions")
        return response.sessions
    }

    func session(id: String) async throws -> SessionDetail {
        try await get("/v1/sessions/\(encoded(id))")
    }

    func createSession(title: String) async throws -> SessionDetail {
        let data = try JSONEncoder().encode(CreateSessionRequest(title: title))
        return try await post("/v1/sessions", data: data)
    }

    func renameSession(id: String, title: String) async throws -> ApiAck {
        let data = try JSONEncoder().encode(RenameRequest(title: title))
        return try await post("/v1/sessions/\(encoded(id))/rename", data: data)
    }

    func deleteSession(id: String) async throws -> ApiAck {
        let request = try makeRequest(path: "/v1/sessions/\(encoded(id))", method: "DELETE")
        return try await perform(request)
    }

    func abortSession(id: String) async throws -> ApiAck {
        let data = Data("{}".utf8)
        return try await post("/v1/sessions/\(encoded(id))/abort", data: data)
    }

    func approve(id: String, approved: Bool, always: Bool) async throws -> ApiAck {
        let data = try JSONEncoder().encode(
            ApprovalRequest(id: id, approved: approved, always: always)
        )
        return try await post("/v1/approvals", data: data)
    }

    func streamTurn(
        prompt: String,
        sessionID: String?,
        model: String?,
        agent: String?,
        effort: String?
    ) -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let payload = TurnRequest(
                        prompt: prompt,
                        sessionID: sessionID,
                        model: model,
                        agent: agent,
                        effort: effort
                    )
                    let data = try JSONEncoder().encode(payload)
                    var request = try self.makeRequest(path: "/v1/turns", method: "POST", body: data)
                    request.timeoutInterval = 3600

                    let (bytes, response) = try await self.session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw RbitClientError.transport("Сервер вернул некорректный ответ.")
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        var errorData = Data()
                        for try await byte in bytes {
                            errorData.append(byte)
                        }
                        throw self.makeHTTPError(status: http.statusCode, data: errorData)
                    }

                    let decoder = JSONDecoder()
                    for try await line in bytes.lines {
                        if Task.isCancelled {
                            throw RbitClientError.cancelled
                        }
                        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !trimmed.isEmpty, let lineData = trimmed.data(using: .utf8) else {
                            continue
                        }
                        if let event = try? decoder.decode(StreamEvent.self, from: lineData) {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish(throwing: RbitClientError.cancelled)
                } catch let error as RbitClientError {
                    continuation.finish(throwing: error)
                } catch let error as URLError where error.code == .cancelled {
                    continuation.finish(throwing: RbitClientError.cancelled)
                } catch {
                    continuation.finish(
                        throwing: RbitClientError.transport(Self.humanMessage(for: error))
                    )
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let request = try makeRequest(path: path)
        return try await perform(request)
    }

    private func post<T: Decodable>(_ path: String, data: Data) async throws -> T {
        let request = try makeRequest(path: path, method: "POST", body: data)
        return try await perform(request)
    }

    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    private func makeRequest(path: String, method: String = "GET", body: Data? = nil) throws -> URLRequest {
        guard let url = URL(string: baseURL.absoluteString + path) else {
            throw RbitClientError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("rbit-ios/1.0", forHTTPHeaderField: "User-Agent")
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue(token, forHTTPHeaderField: "X-RBit-Token")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: request)
        } catch let error as URLError {
            if error.code == .cancelled {
                throw RbitClientError.cancelled
            }
            throw RbitClientError.transport(Self.humanMessage(for: error))
        } catch {
            throw RbitClientError.transport(Self.humanMessage(for: error))
        }
        let data = result.0
        let response = result.1

        guard let http = response as? HTTPURLResponse else {
            throw RbitClientError.transport("Сервер вернул некорректный ответ.")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw makeHTTPError(status: http.statusCode, data: data)
        }
        guard !data.isEmpty else {
            throw RbitClientError.transport("Сервер вернул пустой ответ.")
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw RbitClientError.transport("Не удалось разобрать ответ rbit CLI.")
        }
    }

    private func makeHTTPError(status: Int, data: Data) -> RbitClientError {
        if status == 401 {
            return .unauthorized
        }
        var message = ""
        if let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            if let text = object["error"] as? String {
                message = text
            } else if let nested = object["error"] as? [String: Any],
                      let text = nested["message"] as? String {
                message = text
            }
            if let hint = object["hint"] as? String, !hint.isEmpty {
                message = message.isEmpty ? hint : "\(message) — \(hint)"
            }
        }
        if message.isEmpty, let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) {
            message = String(text.prefix(400))
        }
        switch status {
        case 404:
            if message.isEmpty {
                message = "Метод не найден. Обновите rbit CLI до последней версии."
            }
        case 409:
            if message.isEmpty {
                message = "rbit уже работает над этим чатом."
            }
        case 503:
            if message.isEmpty {
                message = "rbit CLI сейчас недоступен."
            }
        default:
            if message.isEmpty {
                message = "Сервер rbit ответил ошибкой \(status)."
            }
        }
        return .http(status, message)
    }

    private static func humanMessage(for error: Error) -> String {
        if let error = error as? URLError {
            switch error.code {
            case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost:
                return "Не удалось связаться с rbit CLI. Проверьте, что на ПК запущен `rbit mobile`, и телефон в той же Wi-Fi сети."
            case .timedOut:
                return "rbit CLI не отвечает. Проверьте Wi-Fi и запущенный сервер."
            case .notConnectedToInternet:
                return "Нет подключения к сети."
            case .appTransportSecurityRequiresSecureConnection:
                return "iOS заблокировала HTTP-подключение к локальному серверу."
            default:
                return "Ошибка сети: \(error.localizedDescription)"
            }
        }
        return error.localizedDescription
    }
}

private struct SessionsResponse: Decodable {
    let sessions: [SessionSummary]
}

private struct CreateSessionRequest: Encodable {
    let title: String
}

private struct RenameRequest: Encodable {
    let title: String
}

private struct ApprovalRequest: Encodable {
    let id: String
    let approved: Bool
    let always: Bool
}

private struct TurnRequest: Encodable {
    let prompt: String
    let sessionID: String?
    let model: String?
    let agent: String?
    let effort: String?

    enum CodingKeys: String, CodingKey {
        case prompt
        case sessionID = "session_id"
        case model
        case agent
        case effort
    }
}
