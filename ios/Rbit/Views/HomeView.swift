import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var app: AppState
    @State private var prompt = ""

    private let suggestions = [
        "Проверь проект и предложи улучшения",
        "Запусти тесты и исправь ошибки",
        "Сделай git status и объясни изменения",
    ]

    var body: some View {
        ZStack {
            RbitScreenBackground(dots: true)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    hero
                    connectionCard
                    composer
                    if app.isStreaming, let turn = app.activeTurn {
                        runningCard(turn)
                    }
                    if app.connection.isOnline && app.activeTurn == nil {
                        suggestionsView
                    }
                    recents
                    footer
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            BrandMark(size: 19)
            Spacer()
            ConnectionPill()
            Button {
                app.selectedTab = .settings
            } label: {
                AvatarView(
                    name: app.settings.displayName,
                    size: 34,
                    showStatus: app.connection.isOnline
                )
            }
            .buttonStyle(PressableButtonStyle())
            .accessibilityLabel("Профиль и настройки")
        }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .bottom) {
                HeroWordmark(size: 80)
                Spacer(minLength: 12)
                Text("remote")
                    .font(RbitFont.mono(9, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                    .padding(.horizontal, 8)
                    .frame(height: 22)
                    .background(
                        Capsule(style: .continuous)
                            .fill(Color.rbitSurface)
                    )
                    .overlay(
                        Capsule(style: .continuous)
                            .strokeBorder(Color.rbitBorder, lineWidth: 1)
                    )
                    .padding(.bottom, 10)
            }

            Text("терминальный AI-агент на вашем ПК")
                .font(RbitFont.mono(12, weight: .medium))
                .foregroundStyle(Color.rbitAccent)

            Rectangle()
                .fill(
                    LinearGradient(
                        colors: [Color.rbitAccent.opacity(0.7), Color.rbitAccent.opacity(0)],
                        startPoint: .leading,
                        endPoint: .trailing
                    )
                )
                .frame(width: 220, height: 1)

            Text("Опишите задачу — rbit CLI выполнит её на компьютере и вернёт результат сюда.")
                .font(RbitFont.ui(13))
                .foregroundStyle(Color.rbitDim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 12)
    }

    @ViewBuilder
    private var connectionCard: some View {
        switch app.connection {
        case .online:
            EmptyView()
        case .connecting:
            HStack(spacing: 10) {
                ProgressView()
                    .tint(Color.rbitAccent)
                Text("Ищу rbit CLI на компьютере…")
                    .font(RbitFont.ui(12))
                    .foregroundStyle(Color.rbitDim)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .rbitCard(padding: 14, radius: 16)
        case .failed(let message):
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    Image(systemName: "wifi.exclamationmark")
                        .foregroundStyle(Color.rbitRed)
                    Text("Нет связи с rbit CLI")
                        .font(RbitFont.ui(14, weight: .semibold))
                        .foregroundStyle(Color.rbitText)
                }
                Text(message)
                    .font(RbitFont.ui(12))
                    .foregroundStyle(Color.rbitDim)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Повторить") {
                        app.reconnect()
                    }
                    .buttonStyle(RbitGhostButtonStyle())
                    Button("Подключить") {
                        app.isAddServerPresented = true
                    }
                    .buttonStyle(RbitGhostButtonStyle())
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .rbitCard(padding: 14, radius: 16)
        case .disconnected:
            connectCard
        }
    }

    private var connectCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "desktopcomputer")
                    .foregroundStyle(Color.rbitAccent)
                Text("Подключите rbit CLI")
                    .font(RbitFont.ui(14, weight: .semibold))
                    .foregroundStyle(Color.rbitText)
            }
            Text("Запустите сервер на ПК в папке проекта и добавьте его в приложение.")
                .font(RbitFont.ui(12))
                .foregroundStyle(Color.rbitDim)
                .fixedSize(horizontal: false, vertical: true)
            CommandBlock(command: "rbit mobile -d C:\\path\\to\\project")
            HStack(spacing: 8) {
                Button("Добавить ПК") {
                    app.isAddServerPresented = true
                }
                .buttonStyle(RbitGhostButtonStyle())
                Button("Как это работает") {
                    app.isAddServerPresented = true
                }
                .buttonStyle(.plain)
                .font(RbitFont.mono(10, weight: .medium))
                .foregroundStyle(Color.rbitDim)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .rbitCard(padding: 14, radius: 16)
    }

    private var composer: some View {
        ComposerView(
            text: $prompt,
            sessionID: nil,
            placeholder: "Что сделать на компьютере?"
        ) {
            guard app.startTurn(prompt: prompt, sessionID: nil) else { return }
            prompt = ""
        } onStop: {
            app.stopTurn()
        }
        .disabled(!app.connection.isOnline && app.isConfigured)
        .opacity(app.connection.isOnline || !app.isConfigured ? 1 : 0.72)
    }

    private func runningCard(_ turn: ActiveTurn) -> some View {
        Button {
            let title = app.sessions.first(where: { $0.id == turn.sessionID })?.displayTitle ?? "rbit"
            app.openSession(id: turn.sessionID, title: title)
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    Circle()
                        .fill(Color.rbitAccent.opacity(0.14))
                    Circle()
                        .fill(Color.rbitAccent)
                        .frame(width: 7, height: 7)
                        .opacity(0.9)
                }
                .frame(width: 34, height: 34)

                VStack(alignment: .leading, spacing: 3) {
                    Text("rbit работает")
                        .font(RbitFont.ui(13, weight: .semibold))
                        .foregroundStyle(Color.rbitText)
                    Text(turn.statusText.isEmpty ? RbitFormat.truncate(turn.prompt, limit: 52) : turn.statusText)
                        .font(RbitFont.mono(10))
                        .foregroundStyle(Color.rbitDim)
                        .lineLimit(1)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.rbitFaint)
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color.rbitAccent.opacity(0.06))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(Color.rbitAccent.opacity(0.22), lineWidth: 1)
            )
        }
        .buttonStyle(PressableButtonStyle())
    }

    private var suggestionsView: some View {
        VStack(alignment: .leading, spacing: 9) {
            SectionLabel(text: "Быстрый старт")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(suggestions, id: \.self) { suggestion in
                        Button {
                            prompt = suggestion
                            Haptics.tap()
                        } label: {
                            Text(suggestion)
                                .font(RbitFont.ui(11, weight: .medium))
                                .foregroundStyle(Color.rbitDim)
                                .padding(.horizontal, 11)
                                .frame(height: 30)
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
            }
        }
    }

    private var recents: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                SectionLabel(text: "Недавние")
                Spacer()
                if !app.sessions.isEmpty {
                    Button("Все") {
                        app.selectedTab = .history
                    }
                    .font(RbitFont.mono(10, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                    .buttonStyle(.plain)
                }
            }

            if app.recentSessions.isEmpty {
                Text(app.connection.isOnline
                     ? "Пока пусто. Начните с задачи выше."
                     : "История появится после подключения к ПК.")
                    .font(RbitFont.ui(12))
                    .foregroundStyle(Color.rbitFaint)
                    .padding(.vertical, 6)
            } else {
                VStack(spacing: 6) {
                    ForEach(app.recentSessions) { session in
                        Button {
                            app.openSession(session)
                        } label: {
                            HStack(spacing: 11) {
                                Text(String(session.displayTitle.prefix(1)).uppercased())
                                    .font(RbitFont.mono(11, weight: .bold))
                                    .foregroundStyle(Color.rbitText)
                                    .frame(width: 28, height: 28)
                                    .background(
                                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                                            .fill(Color.rbitPanel)
                                    )
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                                            .strokeBorder(Color.rbitBorder, lineWidth: 1)
                                    )
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(session.displayTitle)
                                        .font(RbitFont.ui(12, weight: .medium))
                                        .foregroundStyle(Color.rbitText)
                                        .lineLimit(1)
                                    Text("\(RbitFormat.relative(session.updatedAt ?? session.createdAt)) · \(session.messageCount) сообщ.")
                                        .font(RbitFont.mono(9))
                                        .foregroundStyle(Color.rbitFaint)
                                        .lineLimit(1)
                                }
                                Spacer()
                                if session.isRunning {
                                    ProgressView()
                                        .tint(Color.rbitAccent)
                                        .scaleEffect(0.6)
                                } else {
                                    Image(systemName: "chevron.right")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(Color.rbitFaint)
                                }
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(
                                RoundedRectangle(cornerRadius: 13, style: .continuous)
                                    .fill(Color.rbitSurface)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 13, style: .continuous)
                                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
                            )
                        }
                        .buttonStyle(PressableButtonStyle())
                    }
                }
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 6) {
            Text("rbit CLI")
                .font(RbitFont.mono(9, weight: .medium))
            Text("v\(app.serverVersion)")
                .font(RbitFont.mono(9))
            Spacer()
            Text("локальная сеть")
                .font(RbitFont.mono(9))
        }
        .foregroundStyle(Color.rbitFaint)
        .padding(.top, 6)
    }
}
