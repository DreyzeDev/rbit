import Foundation
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var app: AppState

    @State private var showNameEditor = false
    @State private var nameDraft = ""
    @State private var showModelPicker = false
    @State private var showEffortPicker = false
    @State private var showAgentPicker = false
    @State private var showDisconnect = false

    private var statusColor: Color {
        switch app.connection {
        case .online: return .rbitGreen
        case .connecting: return .rbitYellow
        case .failed: return .rbitRed
        case .disconnected: return .rbitDim
        }
    }

    var body: some View {
        ZStack {
            RbitScreenBackground()

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    profileCard
                    connectionSection
                    defaultsSection
                    appearanceSection
                    serverSection
                    aboutSection
                    footer
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 26)
            }
            .scrollIndicators(.hidden)
        }
        .alert("Как вас называть?", isPresented: $showNameEditor) {
            TextField("Имя", text: $nameDraft)
            Button("Отмена", role: .cancel) {}
            Button("Сохранить") {
                app.setDisplayName(nameDraft)
            }
        } message: {
            Text("Имя и инициалы показываются на аватаре.")
        }
        .confirmationDialog(
            "Отключить ПК?",
            isPresented: $showDisconnect,
            titleVisibility: .visible
        ) {
            Button("Отключить", role: .destructive) {
                app.disconnect()
            }
            Button("Отмена", role: .cancel) {}
        } message: {
            Text("Токен будет удалён с iPhone. Чаты останутся на компьютере.")
        }
        .sheet(isPresented: $showModelPicker) {
            ModelPickerSheet().environmentObject(app)
        }
        .sheet(isPresented: $showEffortPicker) {
            EffortPickerSheet().environmentObject(app)
        }
        .sheet(isPresented: $showAgentPicker) {
            AgentPickerSheet().environmentObject(app)
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Настройки")
                    .font(RbitFont.ui(30, weight: .bold))
                    .foregroundStyle(Color.rbitText)
                Text("rbit CLI · iPhone")
                    .font(RbitFont.mono(10))
                    .foregroundStyle(Color.rbitDim)
            }
            Spacer()
            ConnectionPill()
        }
    }

    private var profileCard: some View {
        HStack(spacing: 14) {
            AvatarView(
                name: app.settings.displayName,
                size: 62,
                showStatus: app.connection.isOnline
            )

            VStack(alignment: .leading, spacing: 4) {
                Text(app.settings.displayName)
                    .font(RbitFont.ui(20, weight: .bold))
                    .foregroundStyle(Color.rbitText)
                HStack(spacing: 5) {
                    Circle()
                        .fill(statusColor)
                        .frame(width: 5, height: 5)
                    Text(app.connection.isOnline ? app.workspaceName : app.connection.label)
                        .font(RbitFont.mono(9))
                        .foregroundStyle(Color.rbitDim)
                }
                Text(app.connection.isOnline ? app.serverPath : "Локальный профиль")
                    .font(RbitFont.mono(8))
                    .foregroundStyle(Color.rbitFaint)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 4)

            IconButton(icon: "pencil", size: 34) {
                nameDraft = app.settings.displayName
                showNameEditor = true
            }
        }
        .rbitCard(padding: 15, radius: 18)
    }

    private var connectionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Подключение")

            if app.isConfigured {
                SettingsSection {
                    settingsRow(
                        icon: "desktopcomputer",
                        title: "Компьютер",
                        subtitle: app.serverStatus?.workspace?.path ?? app.settings.serverURL,
                        value: app.connection.isOnline ? "на связи" : "нет связи",
                        valueColor: statusColor
                    )
                    RbitDivider().padding(.leading, 52)
                    settingsRow(
                        icon: "number",
                        title: "Сервер",
                        subtitle: app.settings.serverURL,
                        value: "v\(app.serverVersion)"
                    )
                    RbitDivider().padding(.leading, 52)
                    settingsRow(
                        icon: "key",
                        title: "Токен",
                        subtitle: "Хранится в Keychain",
                        value: app.hasToken ? "есть" : "нет"
                    )
                    RbitDivider().padding(.leading, 52)
                    settingsRow(
                        icon: "lock.shield",
                        title: "Подтверждение команд",
                        subtitle: "Bash-команды в iPhone",
                        value: app.connection.isOnline
                            ? (app.approvalsEnabled ? "включено" : "выключено")
                            : "—",
                        valueColor: app.connection.isOnline && app.approvalsEnabled
                            ? Color.rbitGreen
                            : Color.rbitDim
                    )

                    HStack(spacing: 8) {
                        Button {
                            Task { await app.refreshAll() }
                        } label: {
                            HStack(spacing: 6) {
                                if app.isRefreshing {
                                    ProgressView()
                                        .tint(Color.rbitText)
                                        .scaleEffect(0.6)
                                }
                                Text("Проверить")
                            }
                        }
                        .buttonStyle(RbitGhostButtonStyle())

                        Button("Изменить") {
                            app.isAddServerPresented = true
                        }
                        .buttonStyle(RbitGhostButtonStyle())

                        Spacer()

                        Button("Отключить") {
                            showDisconnect = true
                        }
                        .font(RbitFont.ui(11, weight: .medium))
                        .foregroundStyle(Color.rbitRed)
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 13)
                }
            } else {
                SettingsSection {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(spacing: 9) {
                            Image(systemName: "desktopcomputer")
                                .foregroundStyle(Color.rbitAccent)
                            Text("ПК не подключён")
                                .font(RbitFont.ui(14, weight: .semibold))
                                .foregroundStyle(Color.rbitText)
                        }
                        Text("Запустите `rbit mobile` на компьютере и добавьте адрес с токеном.")
                            .font(RbitFont.ui(11))
                            .foregroundStyle(Color.rbitDim)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Добавить ПК") {
                            app.isAddServerPresented = true
                        }
                        .buttonStyle(RbitGhostButtonStyle())
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private var defaultsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "По умолчанию")

            SettingsSection {
                settingsButton(
                    icon: "cpu",
                    title: "Модель",
                    subtitle: app.settings.modelID.isEmpty ? "Модель rbit CLI" : app.settings.modelID,
                    value: app.modelChipTitle
                ) {
                    showModelPicker = true
                }
                RbitDivider().padding(.leading, 52)
                settingsButton(
                    icon: "speedometer",
                    title: "Effort",
                    subtitle: app.selectedEffort.subtitle,
                    value: app.selectedEffort.title
                ) {
                    showEffortPicker = true
                }
                RbitDivider().padding(.leading, 52)
                settingsButton(
                    icon: app.settings.agent == "plan" ? "doc.text.magnifyingglass" : "hammer.fill",
                    title: "Агент",
                    subtitle: app.selectedAgent.displayDescription,
                    value: app.selectedAgent.displayTitle,
                    tint: app.settings.agent == "plan" ? .rbitBlue : .rbitAccent
                ) {
                    showAgentPicker = true
                }
            }
        }
    }

    private var appearanceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Оформление")

            SettingsSection {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 9) {
                        Image(systemName: "circle.lefthalf.filled")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(Color.rbitDim)
                            .frame(width: 30)
                        Text("Тема")
                            .font(RbitFont.ui(13, weight: .medium))
                            .foregroundStyle(Color.rbitText)
                        Spacer()
                    }
                    Picker(
                        "Тема",
                        selection: Binding(
                            get: { app.settings.theme },
                            set: { app.setTheme($0) }
                        )
                    ) {
                        ForEach(AppTheme.allCases) { theme in
                            Text(theme.title).tag(theme)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text("rbit Dark — оригинальная палитра терминала: тёплый peach-акцент, графитовые панели.")
                        .font(RbitFont.ui(10))
                        .foregroundStyle(Color.rbitFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(14)
            }
        }
    }

    private var serverSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Сервер rbit")

            VStack(alignment: .leading, spacing: 12) {
                Text("Запуск на компьютере в папке проекта")
                    .font(RbitFont.ui(12, weight: .medium))
                    .foregroundStyle(Color.rbitText)
                CommandBlock(command: "rbit mobile -d C:\\path\\to\\project")
                HStack(spacing: 8) {
                    Button {
                        app.isAddServerPresented = true
                    } label: {
                        Label("Подключить", systemImage: "qrcode.viewfinder")
                    }
                    .buttonStyle(RbitGhostButtonStyle())

                    Spacer()

                    Text("порт 8333")
                        .font(RbitFont.mono(9))
                        .foregroundStyle(Color.rbitFaint)
                }
            }
            .rbitCard(padding: 14, radius: 18)
        }
    }

    private var aboutSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "О приложении")

            SettingsSection {
                settingsRow(
                    icon: "info.circle",
                    title: "rbit для iPhone",
                    subtitle: "Нативный клиент rbit CLI",
                    value: "1.0"
                )
                RbitDivider().padding(.leading, 52)
                if let url = URL(string: "https://github.com/DreyzeDev/rbit") {
                    Link(destination: url) {
                        settingsRow(
                            icon: "chevron.left.forwardslash.chevron.right",
                            title: "Исходники rbit",
                            subtitle: "github.com/DreyzeDev/rbit",
                            value: "GitHub"
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 5) {
            BrandMark(size: 15)
            Text("rbit CLI · remote для iPhone")
                .font(RbitFont.mono(8))
                .foregroundStyle(Color.rbitFaint)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 8)
    }

    private func settingsButton(
        icon: String,
        title: String,
        subtitle: String?,
        value: String,
        tint: Color = .rbitDim,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(tint)
                    .frame(width: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(RbitFont.ui(13, weight: .medium))
                        .foregroundStyle(Color.rbitText)
                    if let subtitle, !subtitle.isEmpty {
                        Text(subtitle)
                            .font(RbitFont.ui(10))
                            .foregroundStyle(Color.rbitDim)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                Text(value)
                    .font(RbitFont.mono(10))
                    .foregroundStyle(Color.rbitDim)
                    .lineLimit(1)
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(Color.rbitFaint)
            }
            .padding(.horizontal, 14)
            .frame(minHeight: 52)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func settingsRow(
        icon: String,
        title: String,
        subtitle: String?,
        value: String,
        valueColor: Color = .rbitDim
    ) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Color.rbitDim)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(RbitFont.ui(13, weight: .medium))
                    .foregroundStyle(Color.rbitText)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(RbitFont.mono(8))
                        .foregroundStyle(Color.rbitFaint)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
            Spacer(minLength: 8)
            Text(value)
                .font(RbitFont.mono(9, weight: .medium))
                .foregroundStyle(valueColor)
                .lineLimit(1)
        }
        .padding(.horizontal, 14)
        .frame(minHeight: 52)
    }
}

struct SettingsSection<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            content
        }
        .background(
            RoundedRectangle(cornerRadius: 17, style: .continuous)
                .fill(Color.rbitSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 17, style: .continuous)
                .strokeBorder(Color.rbitBorder, lineWidth: 1)
        )
    }
}
