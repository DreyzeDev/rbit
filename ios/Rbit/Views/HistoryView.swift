import Foundation
import SwiftUI

struct HistoryView: View {
    @EnvironmentObject private var app: AppState
    @State private var query = ""
    @State private var editingSession: SessionSummary?
    @State private var renameText = ""
    @State private var deletingSession: SessionSummary?

    private var filtered: [SessionSummary] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return app.sessions }
        return app.sessions.filter { session in
            session.displayTitle.lowercased().contains(needle)
                || session.displayPreview.lowercased().contains(needle)
                || (session.model ?? "").lowercased().contains(needle)
        }
    }

    private struct SessionGroup: Identifiable {
        let id: String
        let title: String
        let sessions: [SessionSummary]
    }

    private var groups: [SessionGroup] {
        let grouped = Dictionary(grouping: filtered) { session in
            RbitFormat.groupTitle(for: session.updatedAt ?? session.createdAt)
        }
        return grouped.keys
            .sorted { lhs, rhs in
                let left = grouped[lhs]?.first?.date ?? .distantPast
                let right = grouped[rhs]?.first?.date ?? .distantPast
                if left == right { return lhs < rhs }
                return left > right
            }
            .map { key in
                SessionGroup(id: key, title: key, sessions: grouped[key] ?? [])
            }
    }

    var body: some View {
        ZStack {
            RbitScreenBackground()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    searchField
                    content
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
            .refreshable {
                await app.refreshSessions()
            }
        }
        .alert(
            "Переименовать чат",
            isPresented: Binding(
                get: { editingSession != nil },
                set: { value in
                    if !value { editingSession = nil }
                }
            )
        ) {
            TextField("Название", text: $renameText)
            Button("Отмена", role: .cancel) {
                editingSession = nil
            }
            Button("Сохранить") {
                if let id = editingSession?.id {
                    app.renameSession(id: id, title: renameText)
                }
                editingSession = nil
            }
        } message: {
            Text("Новое имя появится в истории и на ПК.")
        }
        .confirmationDialog(
            "Удалить чат?",
            isPresented: Binding(
                get: { deletingSession != nil },
                set: { value in
                    if !value { deletingSession = nil }
                }
            ),
            titleVisibility: .visible
        ) {
            Button("Удалить", role: .destructive) {
                if let id = deletingSession?.id {
                    app.deleteSession(id: id)
                }
                deletingSession = nil
            }
            Button("Отмена", role: .cancel) {
                deletingSession = nil
            }
        } message: {
            Text(deletingSession?.displayTitle ?? "")
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("История")
                        .font(RbitFont.ui(30, weight: .bold))
                        .foregroundStyle(Color.rbitText)
                    Text("\(RbitFormat.chatCount(app.sessions.count)) · \(app.workspaceName)")
                        .font(RbitFont.mono(10))
                        .foregroundStyle(Color.rbitDim)
                }
                Spacer()
                IconButton(icon: "arrow.clockwise") {
                    Task { await app.refreshAll() }
                }
                Button {
                    app.selectedTab = .new
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(Color(hex: 0x141414))
                        .frame(width: 36, height: 36)
                        .background(
                            Circle()
                                .fill(Color.rbitAccent)
                        )
                }
                .buttonStyle(PressableButtonStyle())
                .accessibilityLabel("Новый чат")
            }
        }
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.rbitDim)
            TextField("Поиск по чатам", text: $query)
                .font(RbitFont.ui(14))
                .foregroundStyle(Color.rbitText)
                .tint(Color.rbitAccent)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            if !query.isEmpty {
                Button {
                    query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.rbitFaint)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 13)
        .frame(height: 42)
        .background(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .fill(Color.rbitSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .strokeBorder(Color.rbitBorder, lineWidth: 1)
        )
    }

    @ViewBuilder
    private var content: some View {
        if !app.isConfigured {
            emptyState(
                icon: "desktopcomputer",
                title: "ПК не подключён",
                text: "Подключите rbit CLI в Настройках, чтобы видеть историю и продолжать чаты.",
                actionTitle: "Открыть настройки"
            ) {
                app.selectedTab = .settings
            }
        } else if !app.connection.isOnline && app.sessions.isEmpty {
            emptyState(
                icon: "wifi.exclamationmark",
                title: "Нет связи с rbit CLI",
                text: "Проверьте подключение и запуск `rbit mobile` в Настройках.",
                actionTitle: "Открыть настройки"
            ) {
                app.selectedTab = .settings
            }
        } else if app.sessions.isEmpty {
            emptyState(
                icon: "clock.arrow.circlepath",
                title: "Пока нет чатов",
                text: "Создайте первый чат и дайте агенту задачу.",
                actionTitle: "Новый чат"
            ) {
                app.selectedTab = .new
            }
        } else if filtered.isEmpty {
            emptyState(
                icon: "magnifyingglass",
                title: "Ничего не найдено",
                text: "Попробуйте другой запрос.",
                actionTitle: nil,
                action: {}
            )
        } else {
            VStack(alignment: .leading, spacing: 20) {
                ForEach(groups) { group in
                    VStack(alignment: .leading, spacing: 8) {
                        SectionLabel(text: group.title)
                        VStack(spacing: 6) {
                            ForEach(group.sessions) { session in
                                sessionRow(session)
                            }
                        }
                    }
                }
            }
        }
    }

    private func sessionRow(_ session: SessionSummary) -> some View {
        HStack(spacing: 8) {
            Button {
                app.openSession(session)
            } label: {
                HStack(spacing: 12) {
                    Text(String(session.displayTitle.prefix(1)).uppercased())
                        .font(RbitFont.mono(12, weight: .bold))
                        .foregroundStyle(Color.rbitText)
                        .frame(width: 34, height: 34)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.rbitPanel)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Color.rbitBorder, lineWidth: 1)
                        )

                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Text(session.displayTitle)
                                .font(RbitFont.ui(13, weight: .semibold))
                                .foregroundStyle(Color.rbitText)
                                .lineLimit(1)
                            if session.isRunning {
                                Circle()
                                    .fill(Color.rbitAccent)
                                    .frame(width: 5, height: 5)
                            }
                        }
                        if !session.displayPreview.isEmpty {
                            Text(session.displayPreview)
                                .font(RbitFont.ui(11))
                                .foregroundStyle(Color.rbitDim)
                                .lineLimit(1)
                        }
                        HStack(spacing: 6) {
                            Text(RbitFormat.relative(session.updatedAt ?? session.createdAt))
                            Text("·")
                            Text("\(session.messageCount) сообщ.")
                            if session.messageCount > 0 || (session.tokens ?? 0) > 0 {
                                Text("·")
                                Text("\(RbitFormat.tokens(session.tokens ?? 0)) tok")
                            }
                            Text("·")
                            Text(RbitFormat.shortModel(session.model))
                        }
                        .font(RbitFont.mono(9))
                        .foregroundStyle(Color.rbitFaint)
                        .lineLimit(1)
                    }

                    Spacer(minLength: 4)

                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.rbitFaint)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(PressableButtonStyle())

            Menu {
                Button {
                    editingSession = session
                    renameText = session.displayTitle
                } label: {
                    Label("Переименовать", systemImage: "pencil")
                }
                Button(role: .destructive) {
                    deletingSession = session
                } label: {
                    Label("Удалить", systemImage: "trash")
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.rbitFaint)
                    .frame(width: 30, height: 34)
                    .contentShape(Rectangle())
            }
        }
        .padding(.leading, 10)
        .padding(.trailing, 7)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: 15, style: .continuous)
                .fill(Color.rbitSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 15, style: .continuous)
                .strokeBorder(Color.rbitBorder, lineWidth: 1)
        )
        .contextMenu {
            Button {
                editingSession = session
                renameText = session.displayTitle
            } label: {
                Label("Переименовать", systemImage: "pencil")
            }
            Button(role: .destructive) {
                deletingSession = session
            } label: {
                Label("Удалить", systemImage: "trash")
            }
        }
    }

    private func emptyState(
        icon: String,
        title: String,
        text: String,
        actionTitle: String?,
        action: @escaping () -> Void
    ) -> some View {
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(Color.rbitFaint)
                .frame(width: 54, height: 54)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color.rbitSurface)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )
            Text(title)
                .font(RbitFont.ui(16, weight: .semibold))
                .foregroundStyle(Color.rbitText)
            Text(text)
                .font(RbitFont.ui(12))
                .foregroundStyle(Color.rbitDim)
                .multilineTextAlignment(.center)
            if let actionTitle {
                Button(action: action) {
                    Text(actionTitle)
                }
                .buttonStyle(RbitGhostButtonStyle())
                .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
