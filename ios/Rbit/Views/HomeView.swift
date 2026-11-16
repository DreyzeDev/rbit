import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var app: AppState
    @State private var prompt = ""

    var body: some View {
        ZStack {
            RbitScreenBackground(dots: true)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    hero
                    composer
                    if app.isStreaming, let turn = app.activeTurn {
                        runningCard(turn)
                    }
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
            Button {
                app.selectedTab = .settings
            } label: {
                AvatarView(name: app.settings.displayName, size: 34)
            }
            .buttonStyle(PressableButtonStyle())
            .accessibilityLabel("Профиль и настройки")
        }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: 12) {
            HeroWordmark(size: 80)

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
}
