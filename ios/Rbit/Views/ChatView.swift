import SwiftUI
import UIKit

struct ChatView: View {
    let route: SessionRoute

    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var text = ""
    @State private var renameText = ""
    @State private var showRename = false
    @State private var showDelete = false
    @State private var followStream = true

    private let bottomID = "chat-bottom"

    private var detail: SessionDetail? {
        guard app.currentDetail?.id == route.id else { return nil }
        return app.currentDetail
    }

    private var messages: [ChatMessage] {
        detail?.messageList ?? []
    }

    private var title: String {
        detail?.displayTitle ?? route.title
    }

    private var active: ActiveTurn? {
        app.turn(for: route.id)
    }

    private var sessionModel: String {
        detail?.model ?? app.settings.modelID
    }

    var body: some View {
        ZStack {
            RbitScreenBackground()

            VStack(spacing: 0) {
                topBar
                messagesArea
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            bottomDock
        }
        .task(id: route.id) {
            if app.currentDetail?.id != route.id {
                await app.loadSession(id: route.id)
            }
        }
        .task(id: detail?.finished) {
            guard detail?.finished == false, active == nil else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { break }
                if app.isStreaming { break }
                await app.loadSession(id: route.id, silent: true)
                if app.currentDetail?.finished != false { break }
            }
        }
        .alert("Переименовать чат", isPresented: $showRename) {
            TextField("Название", text: $renameText)
            Button("Отмена", role: .cancel) {}
            Button("Сохранить") {
                app.renameSession(id: route.id, title: renameText)
            }
        } message: {
            Text("Новое имя появится в истории и на ПК.")
        }
        .confirmationDialog(
            "Удалить чат?",
            isPresented: $showDelete,
            titleVisibility: .visible
        ) {
            Button("Удалить", role: .destructive) {
                app.deleteSession(id: route.id)
                dismiss()
            }
            Button("Отмена", role: .cancel) {}
        } message: {
            Text(title)
        }
    }

    private var topBar: some View {
        HStack(spacing: 10) {
            Button {
                dismiss()
            } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(Color.rbitText)
                    .frame(width: 34, height: 34)
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
            .accessibilityLabel("Закрыть чат")

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(RbitFont.ui(14, weight: .semibold))
                    .foregroundStyle(Color.rbitText)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    if active != nil {
                        PulseDot()
                    }
                    Text(subtitle)
                        .font(RbitFont.mono(9))
                        .foregroundStyle(active != nil ? Color.rbitAccent : Color.rbitDim)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 4)

            Menu {
                Button {
                    renameText = title
                    showRename = true
                } label: {
                    Label("Переименовать", systemImage: "pencil")
                }
                Button {
                    Task {
                        await app.loadSession(id: route.id)
                        await app.refreshSessions()
                    }
                } label: {
                    Label("Обновить", systemImage: "arrow.clockwise")
                }
                if let last = messages.last(where: { $0.isAssistant }), !last.content.isEmpty {
                    Button {
                        UIPasteboard.general.string = last.content
                        app.showToast("Ответ скопирован", .success)
                    } label: {
                        Label("Копировать ответ", systemImage: "doc.on.doc")
                    }
                }
                Divider()
                Button(role: .destructive) {
                    showDelete = true
                } label: {
                    Label("Удалить чат", systemImage: "trash")
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(Color.rbitDim)
                    .frame(width: 34, height: 34)
                    .background(
                        Circle()
                            .fill(Color.rbitSurface)
                    )
                    .overlay(
                        Circle()
                            .strokeBorder(Color.rbitBorder, lineWidth: 1)
                    )
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) {
            RbitDivider()
        }
    }

    private var subtitle: String {
        if let active {
            if !active.statusText.isEmpty { return active.statusText }
            if active.thinking { return "размышляет" }
            if active.tools.contains(where: { $0.isRunning }) { return "выполняет инструменты" }
            return "печатает…"
        }
        if detail?.finished == false { return "выполняется" }
        let model = RbitFormat.shortModel(sessionModel)
        return "\(app.workspaceName) · \(model) · \(messages.count) сообщ."
    }

    private var messagesArea: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if app.isLoadingSession && messages.isEmpty && active == nil {
                        loadingView
                    } else if messages.isEmpty && active == nil {
                        emptyView
                    } else {
                        ForEach(messages) { message in
                            messageView(message)
                        }
                        if let active {
                            StreamingTurnView(turn: active)
                        }
                    }

                    Color.clear
                        .frame(height: 1)
                        .id(bottomID)
                        .onAppear { followStream = true }
                        .onDisappear { if active == nil { followStream = false } }
                }
                .padding(.horizontal, 16)
                .padding(.top, 14)
                .padding(.bottom, 10)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onAppear {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
                    followStream = true
                    proxy.scrollTo(bottomID, anchor: .bottom)
                }
            }
            .onChange(of: messages.count) { _, _ in
                scrollToBottom(proxy)
            }
            .onChange(of: active?.assistantText) { _, _ in
                scrollToBottom(proxy)
            }
            .onChange(of: active?.statusText) { _, _ in
                scrollToBottom(proxy)
            }
            .onChange(of: active?.tools.count) { _, _ in
                scrollToBottom(proxy)
            }
            .overlay(alignment: .bottomTrailing) {
                if !followStream {
                    Button {
                        followStream = true
                        withAnimation(.easeOut(duration: 0.22)) {
                            proxy.scrollTo(bottomID, anchor: .bottom)
                        }
                    } label: {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(Color.rbitText)
                            .frame(width: 34, height: 34)
                            .background(
                                Circle()
                                    .fill(.ultraThinMaterial)
                            )
                            .overlay(
                                Circle()
                                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
                            )
                    }
                    .buttonStyle(PressableButtonStyle())
                    .padding(.trailing, 16)
                    .padding(.bottom, 8)
                    .accessibilityLabel("Вниз")
                }
            }
        }
    }

    private var loadingView: some View {
        HStack(spacing: 10) {
            ProgressView()
                .tint(Color.rbitAccent)
            Text("Загружаю чат…")
                .font(RbitFont.ui(12))
                .foregroundStyle(Color.rbitDim)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    private var emptyView: some View {
        VStack(spacing: 14) {
            BrandMark(size: 35)
            Text("Продолжите диалог")
                .font(RbitFont.ui(17, weight: .semibold))
                .foregroundStyle(Color.rbitText)
            Text("Напишите задачу — rbit выполнит её на ПК в папке проекта.")
                .font(RbitFont.ui(12))
                .foregroundStyle(Color.rbitDim)
                .multilineTextAlignment(.center)
            if !app.connection.isOnline {
                Button("Открыть настройки") {
                    dismiss()
                    app.selectedTab = .settings
                }
                .buttonStyle(RbitGhostButtonStyle())
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 30)
        .padding(.top, 70)
    }

    @ViewBuilder
    private func messageView(_ message: ChatMessage) -> some View {
        if message.isUser {
            UserMessageRow(message: message)
        } else if message.isAssistant {
            AssistantMessageRow(message: message)
        } else {
            HStack(alignment: .top, spacing: 8) {
                Rectangle()
                    .fill(Color.rbitDim)
                    .frame(width: 2)
                Text(message.content)
                    .font(RbitFont.ui(11))
                    .foregroundStyle(Color.rbitDim)
                    .textSelection(.enabled)
            }
            .padding(.vertical, 2)
        }
    }

    private var bottomDock: some View {
        VStack(spacing: 8) {
            if active != nil, let approval = app.pendingApproval {
                ApprovalCard(approval: approval) { approved, always in
                    app.respondToApproval(approved: approved, always: always)
                }
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            if app.isStreaming && active == nil {
                HStack(spacing: 9) {
                    PulseDot()
                    Text("rbit работает в другом чате")
                        .font(RbitFont.mono(10))
                        .foregroundStyle(Color.rbitDim)
                    Spacer()
                    if let turn = app.activeTurn {
                        Button("Открыть") {
                            let sessionTitle = app.sessions
                                .first(where: { $0.id == turn.sessionID })?
                                .displayTitle ?? "rbit"
                            dismiss()
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                                app.openSession(id: turn.sessionID, title: sessionTitle)
                            }
                        }
                        .font(RbitFont.mono(10, weight: .semibold))
                        .foregroundStyle(Color.rbitAccent)
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 12)
                .frame(height: 36)
                .background(
                    Capsule(style: .continuous)
                        .fill(Color.rbitAccent.opacity(0.07))
                )
                .overlay(
                    Capsule(style: .continuous)
                        .strokeBorder(Color.rbitAccent.opacity(0.18), lineWidth: 1)
                )
            }

            if !app.connection.isOnline, app.isConfigured {
                HStack(spacing: 8) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.rbitRed)
                    Text("Нет связи с ПК")
                        .font(RbitFont.mono(10))
                        .foregroundStyle(Color.rbitDim)
                    Spacer()
                    Button("Настройки") {
                        dismiss()
                        app.selectedTab = .settings
                    }
                    .font(RbitFont.mono(10, weight: .semibold))
                    .foregroundStyle(Color.rbitAccent)
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 12)
                .frame(height: 34)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.rbitSurface)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )
            }

            ComposerView(
                text: $text,
                sessionID: route.id,
                placeholder: "Продолжить чат…",
                compact: true
            ) {
                guard app.startTurn(prompt: text, sessionID: route.id) else { return }
                text = ""
            } onStop: {
                app.stopTurn()
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .padding(.bottom, 6)
        .background(
            Rectangle()
                .fill(.ultraThinMaterial)
                .overlay(
                    Rectangle()
                        .fill(Color.rbitBackground.opacity(0.42))
                )
        )
        .overlay(alignment: .top) {
            RbitDivider()
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        guard followStream else { return }
        withAnimation(.easeOut(duration: 0.16)) {
            proxy.scrollTo(bottomID, anchor: .bottom)
        }
    }
}

private struct UserMessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            Spacer(minLength: 44)
            VStack(alignment: .trailing, spacing: 4) {
                Text(message.content)
                    .font(RbitFont.ui(14))
                    .foregroundStyle(Color.rbitText)
                    .textSelection(.enabled)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 9)
                    .background(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(Color.rbitPanelRaised)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .strokeBorder(Color.rbitBorder, lineWidth: 1)
                    )
                if let timestamp = message.timestamp, !timestamp.isEmpty {
                    Text(timestamp)
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitFaint)
                }
            }
        }
    }
}

private struct AssistantMessageRow: View {
    let message: ChatMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                BrandMark(size: 11)
                Text(RbitFormat.shortModel(message.model))
                    .font(RbitFont.mono(8, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                if let duration = RbitFormat.nonEmpty(RbitFormat.duration(message.durationS)) {
                    Text("· \(duration)")
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitFaint)
                }
                Spacer()
                if let tokens = message.tokens, tokens > 0 {
                    Text("\(RbitFormat.tokens(tokens)) tok")
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitFaint)
                }
            }

            ForEach(message.toolCalls ?? []) { call in
                ToolCallCard(
                    name: call.name,
                    arguments: call.arguments,
                    status: call.status,
                    summary: call.summary,
                    output: call.output,
                    diff: call.diff,
                    durationMS: call.durationMS
                )
            }

            if !message.content.isEmpty {
                MarkdownView(text: message.content)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct StreamingTurnView: View {
    let turn: ActiveTurn
    @State private var showReasoning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                PulseDot()
                Text("rbit")
                    .font(RbitFont.mono(9, weight: .semibold))
                    .foregroundStyle(Color.rbitDim)
                if !turn.statusText.isEmpty {
                    Text("· \(turn.statusText)")
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitAccent)
                        .lineLimit(1)
                } else if turn.thinking {
                    Text("· размышляет")
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitDim)
                } else {
                    Text("· печатает")
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitDim)
                }
                Spacer()
            }

            if !turn.reasoningText.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    Button {
                        withAnimation(.easeOut(duration: 0.18)) {
                            showReasoning.toggle()
                        }
                    } label: {
                        HStack(spacing: 7) {
                            Image(systemName: "brain.head.profile")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(Color.rbitViolet)
                            Text("Размышления")
                                .font(RbitFont.mono(9, weight: .medium))
                                .foregroundStyle(Color.rbitDim)
                            Text("\(turn.reasoningText.count) симв.")
                                .font(RbitFont.mono(8))
                                .foregroundStyle(Color.rbitFaint)
                            Spacer()
                            Image(systemName: "chevron.down")
                                .font(.system(size: 7, weight: .bold))
                                .foregroundStyle(Color.rbitFaint)
                                .rotationEffect(.degrees(showReasoning ? 180 : 0))
                        }
                        .padding(.horizontal, 10)
                        .frame(height: 32)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)

                    if showReasoning {
                        RbitDivider()
                            .padding(.horizontal, 10)
                        Text(turn.reasoningText)
                            .font(RbitFont.mono(9.5))
                            .foregroundStyle(Color.rbitDim)
                            .textSelection(.enabled)
                            .padding(10)
                    }
                }
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.rbitPanel.opacity(0.36))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )
            }

            ForEach(turn.tools) { tool in
                ToolCallCard(
                    name: tool.name,
                    arguments: tool.arguments,
                    status: tool.status,
                    summary: tool.summary,
                    output: tool.output,
                    diff: tool.diff,
                    durationMS: tool.durationMS
                )
            }

            if !turn.assistantText.isEmpty {
                MarkdownView(text: turn.assistantText)
            } else if turn.error == nil && turn.tools.isEmpty && turn.reasoningText.isEmpty {
                HStack(spacing: 7) {
                    ProgressView()
                        .tint(Color.rbitAccent)
                        .scaleEffect(0.65)
                    Text(turn.statusText.isEmpty ? "rbit думает…" : turn.statusText)
                        .font(RbitFont.mono(9))
                        .foregroundStyle(Color.rbitDim)
                }
                .padding(.vertical, 3)
            }

            if let error = turn.error {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.rbitRed)
                    Text(error)
                        .font(RbitFont.mono(10))
                        .foregroundStyle(Color.rbitRed)
                        .textSelection(.enabled)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.rbitRed.opacity(0.07))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.rbitRed.opacity(0.24), lineWidth: 1)
                )
            }

            if turn.aborted {
                Text("Остановлено")
                    .font(RbitFont.mono(9))
                    .foregroundStyle(Color.rbitDim)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ApprovalCard: View {
    let approval: PendingApproval
    let respond: (_ approved: Bool, _ always: Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: approval.destructive ? "exclamationmark.shield.fill" : "lock.shield.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(approval.destructive ? Color.rbitRed : Color.rbitAccent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("rbit просит разрешение")
                        .font(RbitFont.ui(13, weight: .semibold))
                        .foregroundStyle(Color.rbitText)
                    Text(approval.tool)
                        .font(RbitFont.mono(9, weight: .medium))
                        .foregroundStyle(approval.destructive ? Color.rbitRed : Color.rbitAccent)
                }
                Spacer()
            }

            Text(approval.input.isEmpty ? "Команда без описания" : approval.input)
                .font(RbitFont.mono(10))
                .foregroundStyle(Color.rbitDim)
                .lineLimit(6)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.rbitCodeBackground)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )

            if approval.destructive {
                Text("Похоже на опасную команду. Проверьте перед разрешением.")
                    .font(RbitFont.ui(10))
                    .foregroundStyle(Color.rbitRed.opacity(0.9))
            }

            HStack(spacing: 8) {
                Button {
                    respond(true, false)
                } label: {
                    Text("Разрешить")
                        .font(RbitFont.ui(12, weight: .semibold))
                        .foregroundStyle(Color(hex: 0x141414))
                        .padding(.horizontal, 14)
                        .frame(height: 34)
                        .background(
                            Capsule(style: .continuous)
                                .fill(Color.rbitAccent)
                        )
                }
                .buttonStyle(PressableButtonStyle())

                Button {
                    respond(true, true)
                } label: {
                    Text("Всегда")
                        .font(RbitFont.ui(12, weight: .medium))
                        .foregroundStyle(Color.rbitText)
                        .padding(.horizontal, 13)
                        .frame(height: 34)
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

                Spacer()

                Button {
                    respond(false, false)
                } label: {
                    Text("Отклонить")
                        .font(RbitFont.ui(12, weight: .medium))
                        .foregroundStyle(Color.rbitRed)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(approval.destructive ? Color.rbitRed.opacity(0.05) : Color.rbitAccent.opacity(0.05))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(
                    approval.destructive ? Color.rbitRed.opacity(0.35) : Color.rbitAccent.opacity(0.3),
                    lineWidth: 1
                )
        )
    }
}

private struct PulseDot: View {
    @State private var pulse = false

    var body: some View {
        Circle()
            .fill(Color.rbitAccent)
            .frame(width: 5, height: 5)
            .scaleEffect(pulse ? 1.45 : 0.8)
            .opacity(pulse ? 0.45 : 1)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
    }
}
