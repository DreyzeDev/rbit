import SwiftUI

struct ModelPickerSheet: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filtered: [ModelInfo] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return app.models }
        return app.models.filter {
            $0.name.lowercased().contains(needle)
                || $0.id.lowercased().contains(needle)
                || $0.provider.lowercased().contains(needle)
        }
    }

    var body: some View {
        VStack(spacing: 16) {
            SheetHeader(
                title: "Модель",
                subtitle: "Сервер: \(app.workspaceName)",
                onClose: { dismiss() }
            )

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Color.rbitDim)
                TextField("Поиск модели", text: $query)
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
            .padding(.horizontal, 12)
            .frame(height: 40)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color.rbitPanel)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Color.rbitBorder, lineWidth: 1)
            )

            ScrollView {
                LazyVStack(spacing: 4) {
                    PickerRow(
                        title: "По умолчанию",
                        subtitle: "Модель из конфига rbit CLI",
                        icon: "terminal",
                        tint: .rbitDim,
                        selected: app.settings.modelID.isEmpty
                    ) {
                        app.setModel("")
                        Haptics.tap()
                        dismiss()
                    }

                    if filtered.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text(app.models.isEmpty ? "Модели не загружены" : "Ничего не найдено")
                                .font(RbitFont.ui(14, weight: .semibold))
                                .foregroundStyle(Color.rbitText)
                            Text("Проверьте связь с ПК или обновите список моделей.")
                                .font(RbitFont.ui(12))
                                .foregroundStyle(Color.rbitDim)
                            Button("Обновить") {
                                Task { await app.refreshAll() }
                            }
                            .buttonStyle(RbitGhostButtonStyle())
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 30)
                    } else {
                        ForEach(filtered) { model in
                            PickerRow(
                                title: model.displayName,
                                subtitle: [model.displayProvider, model.contextLabel]
                                    .compactMap { $0 }
                                    .joined(separator: " · "),
                                icon: "cpu",
                                tint: .rbitAccent,
                                selected: model.id == app.settings.modelID
                            ) {
                                app.setModel(model.id)
                                Haptics.tap()
                                dismiss()
                            }
                        }
                    }
                }
            }
            .scrollIndicators(.hidden)
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 20)
        .background(Color.rbitBackground)
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
    }
}

struct EffortPickerSheet: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 16) {
            SheetHeader(
                title: "Effort",
                subtitle: "Глубина размышлений reasoning-модели",
                onClose: { dismiss() }
            )

            VStack(spacing: 4) {
                ForEach(app.effortOptions) { option in
                    Button {
                        app.setEffort(option.id)
                        Haptics.tap()
                        dismiss()
                    } label: {
                        HStack(spacing: 14) {
                            ZStack {
                                RoundedRectangle(cornerRadius: 11, style: .continuous)
                                    .fill(
                                        option.id == app.settings.effort
                                            ? Color.rbitAccent.opacity(0.13)
                                            : Color.rbitPanel
                                    )
                                if option.id == "auto" {
                                    Image(systemName: "sparkles")
                                        .font(.system(size: 13, weight: .semibold))
                                        .foregroundStyle(Color.rbitAccent)
                                } else {
                                    EffortBars(bars: option.bars)
                                }
                            }
                            .frame(width: 38, height: 38)

                            VStack(alignment: .leading, spacing: 3) {
                                Text(option.title)
                                    .font(RbitFont.ui(15, weight: .semibold))
                                    .foregroundStyle(Color.rbitText)
                                if !option.subtitle.isEmpty {
                                    Text(option.subtitle)
                                        .font(RbitFont.ui(11))
                                        .foregroundStyle(Color.rbitDim)
                                }
                            }

                            Spacer()

                            if option.id == app.settings.effort {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundStyle(Color.rbitAccent)
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                        .background(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .fill(
                                    option.id == app.settings.effort
                                        ? Color.rbitAccent.opacity(0.07)
                                        : Color.clear
                                )
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .strokeBorder(
                                    option.id == app.settings.effort
                                        ? Color.rbitAccent.opacity(0.26)
                                        : Color.clear,
                                    lineWidth: 1
                                )
                        )
                    }
                    .buttonStyle(PressableButtonStyle())
                }
            }

            Text("Effort применяется только к reasoning-моделям. В режиме auto rbit выберет значение сам.")
                .font(RbitFont.ui(11))
                .foregroundStyle(Color.rbitFaint)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .background(Color.rbitBackground)
        .presentationDetents([.medium])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
    }
}

struct AgentPickerSheet: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 16) {
            SheetHeader(
                title: "Режим агента",
                subtitle: "Build меняет файлы. Plan только читает.",
                onClose: { dismiss() }
            )

            VStack(spacing: 6) {
                ForEach(app.agentList) { agent in
                    let isPlan = agent.name == "plan"
                    let tint = isPlan ? Color.rbitBlue : Color.rbitAccent
                    Button {
                        app.setAgent(agent.name)
                        Haptics.tap()
                        dismiss()
                    } label: {
                        HStack(spacing: 14) {
                            Image(systemName: isPlan ? "doc.text.magnifyingglass" : "hammer.fill")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(tint)
                                .frame(width: 42, height: 42)
                                .background(
                                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                                        .fill(tint.opacity(0.12))
                                )

                            VStack(alignment: .leading, spacing: 4) {
                                Text(agent.displayTitle)
                                    .font(RbitFont.ui(15, weight: .semibold))
                                    .foregroundStyle(Color.rbitText)
                                Text(agent.displayDescription)
                                    .font(RbitFont.ui(11))
                                    .foregroundStyle(Color.rbitDim)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                            }

                            Spacer()

                            if agent.name == app.settings.agent {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundStyle(tint)
                            }
                        }
                        .padding(12)
                        .background(
                            RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .fill(Color.rbitSurface)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .strokeBorder(
                                    agent.name == app.settings.agent
                                        ? tint.opacity(0.32)
                                        : Color.rbitBorder,
                                    lineWidth: 1
                                )
                        )
                    }
                    .buttonStyle(PressableButtonStyle())
                }
            }

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .background(Color.rbitBackground)
        .presentationDetents([.medium])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
    }
}
