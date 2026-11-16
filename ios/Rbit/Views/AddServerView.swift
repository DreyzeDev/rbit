import SwiftUI
import UIKit

struct AddServerView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var url = ""
    @State private var token = ""
    @State private var errorMessage: String?
    @State private var isConnecting = false
    @State private var showScanner = false
    @State private var showHowTo = false

    var body: some View {
        ZStack {
            RbitScreenBackground()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    intro
                    formCard
                    if let errorMessage {
                        errorCard(errorMessage)
                    }
                    connectButton
                    howToCard
                    footer
                }
                .padding(.horizontal, 20)
                .padding(.top, 16)
                .padding(.bottom, 28)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
        }
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
        .onAppear {
            if url.isEmpty {
                url = app.settings.serverURL.isEmpty
                    ? RbitRemoteDefaults.serverURL
                    : app.settings.serverURL
            }
            applyPairing(app.incomingPairing)
        }
        .onChange(of: app.incomingPairing) { _, value in
            applyPairing(value)
        }
        .sheet(isPresented: $showScanner) {
            QRScannerSheet { code in
                applyLink(code)
            }
            .environmentObject(app)
        }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 7) {
                BrandMark(size: 26)
                Text("Подключить ПК")
                    .font(RbitFont.ui(25, weight: .bold))
                    .foregroundStyle(Color.rbitText)
            }
            Spacer()
            IconButton(icon: "xmark", size: 34) {
                dismiss()
            }
        }
    }

    private var intro: some View {
        Text("rbit на iPhone управляет rbit CLI на компьютере. Телефон и ПК должны быть в одной Wi-Fi сети.")
            .font(RbitFont.ui(12))
            .foregroundStyle(Color.rbitDim)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var formCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                Button {
                    pasteLink()
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "doc.on.clipboard")
                        Text("Вставить ссылку")
                    }
                }
                .buttonStyle(RbitGhostButtonStyle())

                Button {
                    showScanner = true
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "qrcode.viewfinder")
                        Text("QR")
                    }
                }
                .buttonStyle(RbitGhostButtonStyle())

                Spacer()
            }

            fieldLabel("Адрес сервера")
            TextField("192.168.1.10:8333", text: $url)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .textContentType(.URL)
                .font(RbitFont.mono(13))
                .foregroundStyle(Color.rbitText)
                .tint(Color.rbitAccent)
                .padding(.horizontal, 12)
                .frame(height: 44)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.rbitCodeBackground)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )

            fieldLabel("Токен доступа")
            SecureField("необязательно, если сервер без токена", text: $token)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(RbitFont.mono(12))
                .foregroundStyle(Color.rbitText)
                .tint(Color.rbitAccent)
                .padding(.horizontal, 12)
                .frame(height: 44)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.rbitCodeBackground)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.rbitBorder, lineWidth: 1)
                )

            Text("Токен печатает `rbit mobile` вместе со ссылкой rbit://connect.")
                .font(RbitFont.ui(10))
                .foregroundStyle(Color.rbitFaint)
        }
        .rbitCard(padding: 14, radius: 18)
    }

    private var connectButton: some View {
        Button {
            connect()
        } label: {
            HStack(spacing: 8) {
                if isConnecting {
                    ProgressView()
                        .tint(Color(hex: 0x141414))
                        .scaleEffect(0.75)
                }
                Text(isConnecting ? "Подключаюсь…" : "Подключиться")
            }
        }
        .buttonStyle(RbitPrimaryButtonStyle())
        .disabled(isConnecting)
    }

    private var howToCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                withAnimation(.easeOut(duration: 0.2)) {
                    showHowTo.toggle()
                }
            } label: {
                HStack {
                    Image(systemName: "terminal")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.rbitAccent)
                    Text("Как запустить сервер")
                        .font(RbitFont.ui(13, weight: .semibold))
                        .foregroundStyle(Color.rbitText)
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.rbitFaint)
                        .rotationEffect(.degrees(showHowTo ? 180 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showHowTo {
                VStack(alignment: .leading, spacing: 10) {
                    step(1, "На ПК откройте папку проекта.")
                    step(2, "Запустите сервер для iPhone:")
                    CommandBlock(command: "rbit mobile -d C:\\path\\to\\project")
                    step(3, "Скопируйте ссылку rbit://connect или адрес и токен из терминала.")
                    step(4, "Вставьте в это приложение. Разрешите локальную сеть и камеру при необходимости.")
                    Text("Windows может спросить про доступ в сеть — разрешите для Node/Python. QR-код печатается, если установлен `pip install \"rbit[mobile]\"`.")
                        .font(RbitFont.ui(10))
                        .foregroundStyle(Color.rbitFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .rbitCard(padding: 14, radius: 18)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Работает только в вашей локальной сети.")
                .font(RbitFont.mono(9))
                .foregroundStyle(Color.rbitFaint)
            Text("Сервер rbit по умолчанию подтверждает опасные bash-команды через приложение.")
                .font(RbitFont.ui(10))
                .foregroundStyle(Color.rbitFaint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 2)
    }

    private func fieldLabel(_ text: String) -> some View {
        Text(text.uppercased())
            .font(RbitFont.mono(8, weight: .semibold))
            .tracking(0.8)
            .foregroundStyle(Color.rbitDim)
    }

    private func step(_ number: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Text("\(number)")
                .font(RbitFont.mono(8, weight: .bold))
                .foregroundStyle(Color.rbitBackground)
                .frame(width: 18, height: 18)
                .background(
                    Circle().fill(Color.rbitAccent)
                )
            Text(text)
                .font(RbitFont.ui(11))
                .foregroundStyle(Color.rbitDim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func errorCard(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.rbitRed)
            Text(message)
                .font(RbitFont.ui(11))
                .foregroundStyle(Color.rbitRed)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .fill(Color.rbitRed.opacity(0.07))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .strokeBorder(Color.rbitRed.opacity(0.25), lineWidth: 1)
        )
    }

    private func connect() {
        guard !isConnecting else { return }
        errorMessage = nil
        isConnecting = true
        Task {
            let error = await app.validateAndSave(rawURL: url, token: token)
            isConnecting = false
            if let error {
                errorMessage = error
            } else {
                dismiss()
            }
        }
    }

    private func pasteLink() {
        guard let value = UIPasteboard.general.string, !value.isEmpty else {
            errorMessage = "Буфер обмена пуст."
            return
        }
        applyLink(value)
    }

    private func applyPairing(_ pairing: PairingLink?) {
        guard let pairing else { return }
        url = pairing.url
        if let tokenValue = pairing.token, !tokenValue.isEmpty {
            token = tokenValue
        }
        errorMessage = nil
    }

    private func applyLink(_ raw: String) {
        if let pairing = PairingLink(raw: raw) {
            url = pairing.url
            token = pairing.token ?? token
            errorMessage = nil
            Haptics.tap()
            return
        }
        if RbitClient.parseBaseURL(raw) != nil {
            url = raw
            errorMessage = nil
            Haptics.tap()
            return
        }
        errorMessage = "Не удалось разобрать ссылку. Нужен адрес вида 192.168.1.10:8333 или rbit://connect?…"
    }
}
