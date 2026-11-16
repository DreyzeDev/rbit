import Foundation
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published private(set) var settings: AppSettings
    @Published var selectedTab: AppTab = .new
    @Published private(set) var connection: ConnectionState = .disconnected
    @Published private(set) var serverStatus: ServerStatus?
    @Published private(set) var models: [ModelInfo] = []
    @Published private(set) var availableEfforts: [String] = ["auto", "low", "medium", "high"]
    @Published private(set) var sessions: [SessionSummary] = []
    @Published private(set) var currentDetail: SessionDetail?
    @Published private(set) var isLoadingSession = false
    @Published private(set) var activeTurn: ActiveTurn?
    @Published var presentedSession: SessionRoute?
    @Published private(set) var pendingApproval: PendingApproval?
    @Published private(set) var toast: Toast?
    @Published var isAddServerPresented = false
    @Published private(set) var incomingPairing: PairingLink?
    @Published private(set) var isStartingTurn = false
    @Published private(set) var isStopping = false
    @Published private(set) var isRefreshing = false

    private var token: String?
    private var client: RbitClient?
    private var streamTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var onboardingTask: Task<Void, Never>?

    init() {
        let loaded = AppSettings.load()
        self.settings = loaded
        self.token = loaded.serverURL.isEmpty ? nil : KeychainStore.readToken()
        if loaded.serverURL.isEmpty {
            KeychainStore.deleteToken()
        }

        if loaded.serverURL.isEmpty {
            onboardingTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 750_000_000)
                guard let self, !Task.isCancelled, self.settings.serverURL.isEmpty else { return }
                self.isAddServerPresented = true
            }
        } else {
            Task { [weak self] in
                await self?.bootstrap()
            }
        }
    }

    var preferredColorScheme: ColorScheme? {
        settings.theme.colorScheme
    }

    var isConfigured: Bool {
        !settings.serverURL.isEmpty
    }

    var isStreaming: Bool {
        activeTurn != nil
    }

    var workspaceName: String {
        if let name = serverStatus?.workspace?.name, !name.isEmpty {
            return name
        }
        return "rbit CLI"
    }

    var serverVersion: String {
        serverStatus?.version ?? "—"
    }

    var serverPath: String {
        serverStatus?.workspace?.path ?? "—"
    }

    var hasToken: Bool {
        guard let token else { return false }
        return !token.isEmpty
    }

    var approvalsEnabled: Bool {
        serverStatus?.approvals == true
    }

    var agentList: [AgentInfo] {
        let list = serverStatus?.agents ?? []
        if list.isEmpty {
            return AgentInfo.defaults
        }
        return list
    }

    var effortOptions: [EffortOption] {
        let ids = availableEfforts.isEmpty ? EffortOption.all.map(\.id) : availableEfforts
        return ids.map { id in
            EffortOption.option(for: id)
                ?? EffortOption(id: id, title: id.capitalized, subtitle: "", bars: 0)
        }
    }

    var selectedModel: ModelInfo? {
        models.first(where: { $0.id == settings.modelID })
    }

    var modelChipTitle: String {
        if settings.modelID.isEmpty {
            return "rbit CLI"
        }
        return selectedModel?.displayName ?? RbitFormat.shortModel(settings.modelID)
    }

    var selectedAgent: AgentInfo {
        agentList.first(where: { $0.name == settings.agent })
            ?? AgentInfo(name: settings.agent, title: settings.agent.capitalized, description: "")
    }

    var selectedEffort: EffortOption {
        EffortOption.option(for: settings.effort)
            ?? EffortOption(id: settings.effort, title: settings.effort.capitalized, subtitle: "", bars: 0)
    }

    var recentSessions: [SessionSummary] {
        Array(sessions.prefix(4))
    }

    var chatMessages: [ChatMessage] {
        currentDetail?.messageList ?? []
    }

    func turn(for sessionID: String) -> ActiveTurn? {
        guard let activeTurn, activeTurn.sessionID == sessionID else { return nil }
        return activeTurn
    }

    func displayTitle(for id: String, fallback: String) -> String {
        if currentDetail?.id == id {
            return currentDetail?.displayTitle ?? fallback
        }
        return fallback
    }

    func bootstrap() async {
        guard let base = RbitClient.parseBaseURL(settings.serverURL) else {
            connection = .disconnected
            return
        }
        connection = .connecting
        let candidate = RbitClient(baseURL: base, token: token)
        client = candidate

        do {
            let status = try await candidate.status()
            apply(status: status)
            connection = .online
            settings.hasConnectedOnce = true
            saveSettings()
            startPolling()
            await loadModelsAndSessions(quiet: true)
        } catch {
            handleConnectionFailure(error, quiet: false)
        }
    }

    func validateAndSave(rawURL: String, token rawToken: String) async -> String? {
        let pairing = PairingLink(raw: rawURL)
        let effectiveURL = pairing?.url ?? rawURL
        let effectiveToken = rawToken.isEmpty ? (pairing?.token ?? "") : rawToken
        guard let base = RbitClient.parseBaseURL(effectiveURL) else {
            return "Введите адрес вида 192.168.1.10:8333"
        }
        let cleanToken = effectiveToken.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidate = RbitClient(
            baseURL: base,
            token: cleanToken.isEmpty ? nil : cleanToken
        )

        do {
            _ = try await candidate.health()
            let status = try await candidate.status()
            let modelsResponse = try await candidate.models()
            let loadedSessions = try await candidate.sessions()

            let isNewServer = settings.serverURL != base.absoluteString
            settings.serverURL = base.absoluteString
            settings.hasConnectedOnce = true
            if isNewServer || settings.modelID.isEmpty {
                settings.modelID = modelsResponse.current ?? ""
            }
            if isNewServer, let effort = status.effort, !effort.isEmpty {
                settings.effort = effort
            }
            saveSettings()

            token = cleanToken.isEmpty ? nil : cleanToken
            if let token {
                KeychainStore.saveToken(token)
            } else {
                KeychainStore.deleteToken()
            }

            client = candidate
            apply(status: status)
            models = modelsResponse.models
            updateEfforts(from: status.efforts ?? modelsResponse.efforts)
            sessions = Self.sorted(loadedSessions)
            connection = .online
            isAddServerPresented = false
            incomingPairing = nil
            startPolling()
            showToast("Подключено к \(workspaceName)", .success)
            Haptics.success()
            return nil
        } catch {
            return RbitFormat.readableError(error)
        }
    }

    func refreshAll(quiet: Bool = false) async {
        guard let client else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            let status = try await client.status()
            apply(status: status)
            connection = .online
            startPolling()
        } catch {
            handleConnectionFailure(error, quiet: quiet)
            return
        }

        await loadModelsAndSessions(quiet: quiet)

        if let id = presentedSession?.id, activeTurn == nil {
            await loadSession(id: id, silent: true)
        }
    }

    func refreshSessions() async {
        guard let client else { return }
        do {
            sessions = Self.sorted(try await client.sessions())
        } catch {
            if let rbitError = error as? RbitClientError, rbitError.isCancellation {
                return
            }
            // Session refresh failures are non-fatal while the app is open.
        }
    }

    func reconnect() {
        guard !settings.serverURL.isEmpty else {
            isAddServerPresented = true
            return
        }
        Task { [weak self] in
            await self?.refreshAll(quiet: false)
        }
    }

    func disconnect() {
        pollTask?.cancel()
        pollTask = nil
        if let turn = activeTurn, let client {
            Task {
                _ = try? await client.abortSession(id: turn.sessionID)
            }
        }
        streamTask?.cancel()
        streamTask = nil
        KeychainStore.deleteToken()
        token = nil
        client = nil
        activeTurn = nil
        pendingApproval = nil
        presentedSession = nil
        currentDetail = nil
        serverStatus = nil
        models = []
        sessions = []
        isStartingTurn = false
        isStopping = false
        connection = .disconnected
        settings.serverURL = ""
        settings.modelID = ""
        settings.hasConnectedOnce = false
        saveSettings()
        isAddServerPresented = true
    }

    func setModel(_ id: String) {
        settings.modelID = id
        saveSettings()
    }

    func setEffort(_ id: String) {
        settings.effort = id
        saveSettings()
    }

    func setAgent(_ id: String) {
        settings.agent = id
        saveSettings()
    }

    func setDisplayName(_ name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.displayName = trimmed.isEmpty ? "rbit user" : trimmed
        saveSettings()
    }

    func setTheme(_ theme: AppTheme) {
        settings.theme = theme
        saveSettings()
    }

    func handlePairingURL(_ url: URL) {
        guard let link = PairingLink(url: url) else { return }
        incomingPairing = link
        isAddServerPresented = true
    }

    func openSession(_ summary: SessionSummary) {
        let route = SessionRoute(id: summary.id, title: summary.displayTitle)
        presentedSession = route
        if currentDetail?.id != summary.id {
            currentDetail = nil
        }
        Task { [weak self] in
            await self?.loadSession(id: summary.id)
        }
    }

    func openSession(id: String, title: String) {
        presentedSession = SessionRoute(id: id, title: title)
        if currentDetail?.id != id {
            currentDetail = nil
        }
        Task { [weak self] in
            await self?.loadSession(id: id)
        }
    }

    func loadSession(id: String, silent: Bool = false) async {
        guard let client else { return }
        isLoadingSession = true
        defer { isLoadingSession = false }
        do {
            let detail = try await client.session(id: id)
            if presentedSession?.id == id || presentedSession == nil {
                currentDetail = detail
            }
            if var route = presentedSession, route.id == id, route.title != detail.displayTitle {
                route.title = detail.displayTitle
                presentedSession = route
            }
        } catch {
            if !silent {
                showToast(RbitFormat.readableError(error), .error)
            }
        }
    }

    @discardableResult
    func startTurn(prompt rawPrompt: String, sessionID: String?) -> Bool {
        let prompt = rawPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return false }
        guard let client else {
            isAddServerPresented = true
            return false
        }
        if activeTurn != nil || isStartingTurn {
            showToast("rbit уже работает. Дождитесь ответа или нажмите «Стоп».", .info)
            return false
        }

        isStartingTurn = true
        Haptics.tap()

        streamTask = Task { [weak self] in
            guard let self else { return }
            var targetSessionID = sessionID
            do {
                if targetSessionID == nil {
                    let created = try await client.createSession(title: String(prompt.prefix(48)))
                    targetSessionID = created.id
                    self.currentDetail = created
                    self.presentedSession = SessionRoute(
                        id: created.id,
                        title: created.displayTitle
                    )
                }

                guard let targetSessionID else {
                    self.isStartingTurn = false
                    return
                }

                if self.currentDetail?.id != targetSessionID {
                    await self.loadSession(id: targetSessionID)
                }
                guard self.currentDetail?.id == targetSessionID else {
                    self.isStartingTurn = false
                    self.showToast("Не удалось загрузить чат.", .error)
                    return
                }

                self.appendOptimisticUserMessage(prompt, sessionID: targetSessionID)
                self.activeTurn = ActiveTurn(sessionID: targetSessionID, prompt: prompt)
                self.isStartingTurn = false
                if var route = self.presentedSession, route.id == targetSessionID {
                    route.title = self.currentDetail?.displayTitle ?? route.title
                    self.presentedSession = route
                } else {
                    self.presentedSession = SessionRoute(
                        id: targetSessionID,
                        title: self.currentDetail?.displayTitle ?? "rbit"
                    )
                }

                let stream = client.streamTurn(
                    prompt: prompt,
                    sessionID: targetSessionID,
                    model: self.settings.modelID.isEmpty ? nil : self.settings.modelID,
                    agent: self.settings.agent,
                    effort: self.settings.effort
                )
                for try await event in stream {
                    if Task.isCancelled { break }
                    self.handle(event: event)
                }
                await self.completeTurn(sessionID: targetSessionID)
            } catch {
                self.isStartingTurn = false
                if let rbitError = error as? RbitClientError, rbitError.isCancellation {
                    await self.completeTurn(
                        sessionID: targetSessionID ?? self.activeTurn?.sessionID ?? "",
                        cancelled: true
                    )
                } else {
                    self.handleTurnFailure(error, sessionID: targetSessionID)
                }
            }
        }
        return true
    }

    func stopTurn() {
        guard let turn = activeTurn, let client, !isStopping else { return }
        isStopping = true
        Haptics.tap()
        let sessionID = turn.sessionID
        Task { [weak self] in
            guard let self else { return }
            _ = try? await client.abortSession(id: sessionID)
            if var current = self.activeTurn, current.sessionID == sessionID {
                current.aborted = true
                self.activeTurn = current
            }
            self.streamTask?.cancel()
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if self.activeTurn?.sessionID == sessionID {
                self.activeTurn = nil
                self.pendingApproval = nil
                self.isStopping = false
                await self.refreshSessions()
                if self.presentedSession?.id == sessionID || self.currentDetail?.id == sessionID {
                    await self.loadSession(id: sessionID)
                }
            }
        }
    }

    func respondToApproval(approved: Bool, always: Bool) {
        guard let approval = pendingApproval, let client else { return }
        pendingApproval = nil
        Haptics.tap()
        Task { [weak self] in
            do {
                _ = try await client.approve(
                    id: approval.id,
                    approved: approved,
                    always: always
                )
            } catch {
                self?.showToast(RbitFormat.readableError(error), .error)
            }
        }
    }

    func renameSession(id: String, title: String) {
        guard let client else { return }
        let cleanTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanTitle.isEmpty else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                _ = try await client.renameSession(id: id, title: cleanTitle)
                await self.refreshSessions()
                if var detail = self.currentDetail, detail.id == id {
                    detail.title = cleanTitle
                    self.currentDetail = detail
                }
                if var route = self.presentedSession, route.id == id {
                    route.title = cleanTitle
                    self.presentedSession = route
                }
                self.showToast("Название обновлено", .success)
            } catch {
                self.showToast(RbitFormat.readableError(error), .error)
            }
        }
    }

    func deleteSession(id: String) {
        guard let client else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                _ = try await client.deleteSession(id: id)
                self.sessions.removeAll(where: { $0.id == id })
                if self.presentedSession?.id == id {
                    self.presentedSession = nil
                }
                if self.currentDetail?.id == id {
                    self.currentDetail = nil
                }
                self.showToast("Чат удалён", .success)
            } catch {
                self.showToast(RbitFormat.readableError(error), .error)
            }
        }
    }

    func appDidBecomeActive() {
        guard isConfigured else { return }
        Task { [weak self] in
            await self?.refreshAll(quiet: true)
        }
    }

    func showToast(_ message: String, _ kind: Toast.Kind = .info) {
        toastTask?.cancel()
        toast = Toast(message: message, kind: kind)
        if kind == .error {
            Haptics.error()
        }
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_400_000_000)
            guard !Task.isCancelled else { return }
            self?.toast = nil
        }
    }

    private func apply(status: ServerStatus) {
        serverStatus = status
        updateEfforts(from: status.efforts)
    }

    private func updateEfforts(from efforts: [String]?) {
        guard let efforts, !efforts.isEmpty else { return }
        availableEfforts = efforts
        if !efforts.contains(settings.effort), let first = efforts.first {
            settings.effort = first
            saveSettings()
        }
    }

    private func loadModelsAndSessions(quiet: Bool) async {
        guard let client else { return }
        do {
            let response = try await client.models()
            models = response.models
            updateEfforts(from: response.efforts)
            if settings.modelID.isEmpty, let current = response.current, !current.isEmpty {
                settings.modelID = current
                saveSettings()
            }
        } catch {
            if !quiet {
                showToast("Не удалось загрузить модели.", .error)
            }
        }

        do {
            sessions = Self.sorted(try await client.sessions())
        } catch {
            if !quiet {
                showToast("Не удалось загрузить историю.", .error)
            }
        }
    }

    private func handleConnectionFailure(_ error: Error, quiet: Bool) {
        let message = RbitFormat.readableError(error)
        connection = .failed(message)
        if !quiet {
            showToast(message, .error)
        }
    }

    private func appendOptimisticUserMessage(_ text: String, sessionID: String) {
        guard var detail = currentDetail, detail.id == sessionID else { return }
        var messages = detail.messages ?? []
        messages.append(
            ChatMessage(
                role: "user",
                content: text,
                timestamp: RbitFormat.time(Date())
            )
        )
        detail.messages = messages
        currentDetail = detail
    }

    private func handle(event: StreamEvent) {
        guard var turn = activeTurn else { return }
        switch event.type {
        case "session":
            if let title = event.title, !title.isEmpty {
                if var detail = currentDetail, detail.id == turn.sessionID {
                    detail.title = title
                    currentDetail = detail
                }
                if var route = presentedSession, route.id == turn.sessionID {
                    route.title = title
                    presentedSession = route
                }
            }
        case "status":
            turn.statusText = event.text ?? ""
        case "thinking":
            turn.thinking = event.active ?? false
        case "reasoning":
            if let piece = event.text {
                turn.reasoningText += piece
            }
        case "delta":
            if let text = event.text {
                turn.assistantText = text
            }
        case "tool_call":
            let id = event.id ?? UUID().uuidString
            let tool = LiveTool(
                id: id,
                name: event.name ?? "tool",
                arguments: event.arguments,
                status: "running",
                summary: nil,
                output: nil,
                diff: nil,
                durationMS: nil
            )
            turn.tools.append(tool)
        case "tool_result":
            let index = turn.tools.lastIndex(where: { tool in
                if let id = event.id, tool.id == id { return true }
                return tool.name == event.name && tool.isRunning
            })
            if let index {
                turn.tools[index].status = event.status ?? "success"
                turn.tools[index].summary = event.summary
                turn.tools[index].output = event.output
                turn.tools[index].diff = event.diff
                turn.tools[index].durationMS = event.durationMS
            } else {
                turn.tools.append(
                    LiveTool(
                        id: event.id ?? UUID().uuidString,
                        name: event.name ?? "tool",
                        arguments: event.arguments,
                        status: event.status ?? "success",
                        summary: event.summary,
                        output: event.output,
                        diff: event.diff,
                        durationMS: event.durationMS
                    )
                )
            }
        case "tool_approval":
            pendingApproval = PendingApproval(
                id: event.id ?? UUID().uuidString,
                tool: event.tool ?? "bash",
                input: event.input ?? "",
                destructive: event.destructive ?? false
            )
            Haptics.warning()
        case "message":
            if let content = event.content {
                turn.assistantText = content
            }
            turn.thinking = false
            turn.statusText = ""
        case "aborted":
            turn.aborted = true
            turn.thinking = false
        case "error":
            turn.error = event.text ?? "Ошибка rbit"
            turn.thinking = false
        case "done":
            turn.thinking = false
        default:
            break
        }
        activeTurn = turn
    }

    private func completeTurn(sessionID: String, cancelled: Bool = false) async {
        let turn = activeTurn
        let isCurrent = turn == nil || turn?.sessionID == sessionID
        let wasAborted = cancelled || turn?.aborted == true
        let error = turn?.error
        if isCurrent {
            pendingApproval = nil
            activeTurn = nil
        }
        isStartingTurn = false
        isStopping = false

        await refreshSessions()

        if !sessionID.isEmpty,
           presentedSession?.id == sessionID || currentDetail?.id == sessionID {
            await loadSession(id: sessionID, silent: true)
        }

        if let error, !wasAborted {
            showToast(error, .error)
        } else if wasAborted {
            showToast("Остановлено", .info)
        } else {
            Haptics.success()
        }
    }

    private func handleTurnFailure(_ error: Error, sessionID: String?) {
        let message = RbitFormat.readableError(error)
        if let sessionID, activeTurn?.sessionID != sessionID, activeTurn != nil {
            showToast(message, .error)
            return
        }
        activeTurn = nil
        pendingApproval = nil
        isStopping = false
        isStartingTurn = false
        showToast(message, .error)
        if let sessionID {
            Task { [weak self] in
                guard let self else { return }
                await self.refreshSessions()
                if self.currentDetail?.id == sessionID {
                    await self.loadSession(id: sessionID, silent: true)
                }
            }
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                guard let self, !Task.isCancelled else { return }
                if self.activeTurn == nil, self.connection.isOnline {
                    await self.pollOnce()
                }
            }
        }
    }

    private func pollOnce() async {
        guard let client else { return }
        do {
            let status = try await client.status()
            apply(status: status)
            connection = .online
            sessions = Self.sorted(try await client.sessions())
        } catch {
            if let rbitError = error as? RbitClientError, rbitError.isCancellation {
                return
            }
            if connection.isOnline {
                connection = .failed(RbitFormat.readableError(error))
            }
        }
    }

    private func saveSettings() {
        settings.save()
    }

    private static func sorted(_ sessions: [SessionSummary]) -> [SessionSummary] {
        sessions.sorted { lhs, rhs in
            let left = lhs.date ?? .distantPast
            let right = rhs.date ?? .distantPast
            return left > right
        }
    }
}


