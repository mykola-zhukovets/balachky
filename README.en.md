<p align="center"><strong>English</strong> · <a href="README.uk.md">Українська</a></p>

<p align="center">
  <img src="assets/mascot-512.png" alt="Balachky beetle mascot with a microphone" width="150">
</p>

<h1 align="center">Balachky</h1>

<p align="center">Record meetings, dictate into other programs, and transcribe audio files on Windows.</p>

<p align="center"><sub>“Balachky” is Ukrainian for casual chats; Korosten is the town in Ukraine where the app is made.</sub></p>

<p align="center">
  <img src="https://img.shields.io/badge/release-v1.2.4.3--beta-1f6feb" alt="Release v1.2.4.3-beta">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-6e7681" alt="Windows 10 and 11">
  <img src="https://img.shields.io/badge/processing-local-2ea043" alt="Local processing">
  <img src="https://img.shields.io/badge/license-source--available-8957e5" alt="Source-available license">
</p>

<p align="center">
  <a href="https://github.com/mykola-zhukovets/balachky/releases/download/v1.2.4.3-beta/BalachkySetup-1.2.4.3-beta-2554A930.exe"><img src="https://img.shields.io/badge/Download_for_Windows_(.exe)-2ea043?style=for-the-badge" alt="Download Balachky v1.2.4.3 beta for Windows" height="44"></a>
  <br>
  <a href="https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.4.3-beta">Release details</a>
  <br>
  <sub>Windows 10/11 · 64-bit · no account required</sub>
  <br>
  <sub><strong>Unsigned beta:</strong> Windows may show a warning during installation.</sub>
</p>

Balachky keeps recording and speech recognition on your computer. Once you have downloaded or imported the models and optional components you need, the main features can work without an internet connection.

**100 speech-recognition languages in v1.2.4.3-beta · Automatic language detection · Interface in English and Ukrainian**

<a id="usage"></a>

## Meetings

Choose a recording preset for the situation:

- **In-person meeting:** one microphone records the conversation in the room.
- **Online call:** Balachky records your microphone and Windows system sound as separate, synchronized tracks.
- **Multiple microphones:** use two to four microphones, with optional Windows system sound.

Balachky does not join Teams, Zoom, or a browser call as a bot. It records the audio sources you select. There is no live meeting transcript. After you stop recording, Balachky saves and prepares the meeting audio. You decide when to start transcription.

Separate tracks make the result easier to review. In the player, you can adjust the volume of each source, mute it, or listen to it by itself while the tracks stay synchronized. The balance you set can be saved as a separate WAV file; the original tracks are left untouched.

While recording, a button on the meeting page or `Ctrl+Alt+B` marks an important moment without interrupting the recording. Marks appear as a list in playback, and clicking one jumps straight to it. In an open meeting, `Ctrl+F` searches the transcript: matches are highlighted and `Enter` moves to the next one.

A deleted meeting first goes to a trash bin and stays there for 7 days — you can restore it from the notification at the bottom of the screen.

Optional speaker separation (diarization) can mark speakers in the transcript of the Windows system-sound track. You can then replace generic labels with names. If this component is not installed, the meeting can still be recorded and transcribed.

The optional local AI protocol turns a completed transcript into a **draft for review** with a summary, decisions, tasks, supporting excerpts, and timestamps. It is not a final record, and important details should be checked against the recording.

> Balachky does not notify participants or collect their consent. Before recording, you are responsible for telling the people involved and following the rules that apply where you are.

Screen recording is a separate option and is not included in a meeting by default. You can explicitly enable it for a meeting when you need it.

## Dictation

Use Balachky in the program you are already working in. Start and stop with a recording shortcut or a side mouse button. When the text is ready, Balachky can paste it into the active field, keep it in its own window, or do both.

Choose how the recording shortcut works: hold it while speaking, press once to start and once to stop, or use a double press for hands-free recording. If another dictation starts while the previous one is still being processed, the queue preserves the order.

An optional preview lets you check the text before it is pasted. You can also pin the target window so a change of focus does not send text to another program. If clipboard restore is enabled, Balachky restores the previous clipboard text after pasting.

<p align="center">
  <img src="docs/screenshots/01-dictation-en.png" alt="Dictation page with completed dictation cards" width="820">
  <br>
  <sub>Speak in one program and receive the text in the field where you are working.</sub>
</p>

## Audio files

Add existing recordings to a queue and transcribe them locally. Each item shows its progress and can be cancelled separately. You can transcribe a file again with another installed model; change the recognition language in Settings when needed.

The same page includes a recorder and player for checking the source audio. Finished transcripts can be copied or exported as TXT, Markdown, SRT, VTT, or DOCX.

<p align="center">
  <img src="docs/screenshots/02-files-en.png" alt="Audio files page with a transcription queue, recorder, player, and progress" width="820">
  <br>
  <sub>Record or add an audio file, follow its progress, and export the transcript from one page.</sub>
</p>

## More tools

### 1. Dictionaries and learning

Keep separate dictionaries for different contexts. Import or export them, and let Balachky learn from a correction only after you confirm it.

### 2. History and exports

Dictation history stays on your computer. Search earlier dictations, review statistics, copy or correct the text, delete entries, and export the results you need.

### 3. Offline package

The model manager shows which recognition models are installed, which one is active, and how much disk space they use. Build an offline package to move the required models and components to another Windows computer.

### 4. Screen recording

Record an entire monitor or one window from a separate work mode. In v1.2.4.3-beta, the “Area” option still records the first monitor rather than a custom crop.

### 5. Whisper without the command line

Choose recordings, a Whisper model, and a language in the Balachky window. Transcribe locally and export the result without terminal commands or scripts.

## Advanced and optional

- **Meeting protection:** completed meeting files can be encrypted with a separate key. Working files may remain unencrypted while recording is in progress, so the current protection state matters. Integrity records and export tools are available for later verification.
- **Local AI protocol:** uses a separate local model and component. Its result is a draft that needs review.
- **Read aloud:** the interface is present, but the speech engine is not included in the public v1.2.4.3-beta installer.
- **Voice navigation:** built-in commands let you move between supported document fields and cells. Voice editing depends on a separate local component.
- **Command line and MCP:** the CLI and MCP server are tools for running Balachky from source. They are not standard features of the Windows installer. See [MCP server documentation](docs/MCP-SERVER.md).

## Local and offline use

| Action | Internet use |
|---|---|
| Record, transcribe, dictate, and process meetings | No, after the required models and components are available |
| Setup-wizard connection check | v1.2.4.3-beta makes a TCP connection to `1.1.1.1:53` when the setup wizard opens; the check does not send voice or transcripts |
| Download a recognition model | When you choose one during setup or later |
| Download an optional component | When you select it during setup or enable it later |
| Check for updates | When you check manually; periodically only if automatic checks are enabled |
| Download an app update | When you accept an update, or automatically if you separately enable background downloads |

Balachky does not send your voice, audio files, recordings, transcripts, dictionaries, or AI protocol drafts to a server for processing. The app does not require an account.

Your data is stored on your computer. See [Data and privacy](docs/DATA-PRIVACY.md) for the folders Balachky uses, the network boundaries, and removal instructions.

### What Balachky does not do

- It is not a cloud transcription service and does not require an account.
- It does not join an online call as a bot.
- It does not notify meeting participants or collect their consent.
- Transcripts and AI protocol drafts are not final records. Review important details against the recording.
- It does not include screen recording in Meetings by default. You have to enable that option explicitly.

## Installation

Balachky requires 64-bit Windows 10 or 11. The installer size is 161.8 MiB (169.6 MB; 169,609,456 bytes). Recognition models and optional components need additional disk space; the total depends on what you install.

Download `BalachkySetup-1.2.4.3-beta-2554A930.exe` from the [official v1.2.4.3-beta release](https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.4.3-beta) and run it. A model download requires an internet connection unless you import the files from an offline package.

### Windows SmartScreen and file verification

The current beta installer does not have a digital signature, so Windows SmartScreen may show an “Unknown publisher” warning. Download the installer only from this repository’s GitHub Releases. A [step-by-step SmartScreen guide](docs/INSTALL-SMARTSCREEN.md) is available.

SHA-256 for this exact file:

`2554A930B2BB8DA1EA27790907E0656EB8D7CD60859E3193B32926D3FA74371E`

A matching SHA-256 confirms that your download is byte-for-byte identical to the published file. It does not, by itself, prove that a file is safe.

Check this file on VirusTotal by its SHA-256: [file page](https://www.virustotal.com/gui/file/2554a930b2bb8da1ea27790907e0656eb8d7cd60859e3193b32926d3fa74371e/detection). If no report exists yet, you can submit the file yourself. A scan is an additional signal, not a substitute for verifying the SHA-256.

## Beta and commercial use

Balachky is source-available under the [PolyForm Noncommercial 1.0.0](LICENSE). Noncommercial use of every mode is free. During the beta, commercial use of every mode is also free.

After the beta, these remain free for commercial use:

- Dictation
- Dictionaries and learning
- Whisper model management
- Dictation history and exports
- Offline package

Commercial use of Meetings, Audio files, Screen recording, and the AI protocol will require a separate license after the beta. Pricing and payment terms have not been decided.

The beta has no scheduled end date. A 30-day transition period begins only after Mykola Zhukovets explicitly announces both the first stable release and the end of the beta in a GitHub Release.

This section is a short explanation. The detailed terms are in [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

Third-party components keep their own licenses. See [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt).

## Help and project links

- Found a bug? [Open an Issue](https://github.com/mykola-zhukovets/balachky/issues) and include the app version and what you were doing.
- Have a question or an idea? Use [GitHub Discussions](https://github.com/mykola-zhukovets/balachky/discussions).
- Found a security problem? Follow the private reporting instructions in [SECURITY.md](SECURITY.md).
- Want to help? Read [CONTRIBUTING.md](CONTRIBUTING.md).
- Looking for changes between versions? See the [Changelog](CHANGELOG.md).

## Support the project

If Balachky is useful to you, you can support its development. Support is optional. It does not include a commercial license, unlock features, or change the order in which support requests are handled.

[Monobank (UAH)](https://send.monobank.ua/jar/21rfey7KTz) · [PrivatBank (USD)](https://www.privat24.ua/send/4h4jh) · [PrivatBank (EUR)](https://www.privat24.ua/send/4h5jr)

USDT (TRC-20): `TTsc47PDTe2rUkeXcZGTQwR6driykkP2s8`

<details>
<summary>BTC and ETH</summary>

- BTC: `bc1q8wqskryef3ey09jxhv9epdv7kpxnzg8vcf40hy`
- ETH: `0x6A9FeF1CB66C20D31f770a970F790aFC85243A57`

</details>

<p align="center"><sub>Made in Korosten, Ukraine.</sub></p>
