import { Heart } from "lucide-react";

const GithubIcon = ({ className }: { className?: string }) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.56v-2.01c-3.2.7-3.88-1.54-3.88-1.54-.52-1.33-1.28-1.68-1.28-1.68-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.03 1.76 2.7 1.25 3.36.96.1-.74.4-1.25.73-1.54-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.05 0 0 .96-.31 3.16 1.18a11.04 11.04 0 0 1 5.74 0c2.19-1.49 3.15-1.18 3.15-1.18.63 1.59.24 2.76.12 3.05.74.81 1.18 1.84 1.18 3.1 0 4.42-2.69 5.4-5.26 5.68.41.36.78 1.07.78 2.15v3.18c0 .31.21.67.8.55C20.71 21.39 24 17.08 24 12 24 5.65 18.85.5 12 .5Z"/>
  </svg>
);

export default function AboutPage() {
  return (
    <div className="px-8 py-12">
      <div className="max-w-2xl mx-auto space-y-10">
        {/* Header */}
        <div className="space-y-3 text-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo-wordmark.png" alt="SovLens" className="h-28 mx-auto" />
          <p
            className="text-lg leading-relaxed"
            style={{ color: "var(--muted-text)" }}
          >
            Search your media like you search the web.
          </p>
        </div>

        {/* What is it? */}
        <section className="space-y-3">
          <h2
            className="text-xl font-medium"
            style={{ color: "var(--foreground)" }}
          >
            What is it?
          </h2>
          <p
            className="text-base leading-relaxed"
            style={{ color: "var(--foreground)", opacity: 0.8 }}
          >
            SovLens is a private, local AI engine that understands your photos
            and videos. Drop a folder and ask things like{" "}
            <em>&ldquo;sunset on the beach&rdquo;</em> or{" "}
            <em>&ldquo;the dog jumping&rdquo;</em>. No cloud. No accounts. Your
            files never leave your machine.
          </p>
        </section>

        {/* How does it work? */}
        <section className="space-y-3">
          <h2
            className="text-xl font-medium"
            style={{ color: "var(--foreground)" }}
          >
            How does it work?
          </h2>
          <p
            className="text-base leading-relaxed"
            style={{ color: "var(--foreground)", opacity: 0.8 }}
          >
            A neural network (CLIP) looks at each frame and translates the
            visual content into a kind of fingerprint. When you search, your
            text becomes the same kind of fingerprint, and we find the closest
            matches. Voice tracks in videos are transcribed locally with
            Whisper, so spoken words are searchable too.
          </p>
        </section>

        {/* Privacy by design */}
        <section className="space-y-3">
          <h2
            className="text-xl font-medium"
            style={{ color: "var(--foreground)" }}
          >
            Privacy by design
          </h2>
          <ul
            className="list-disc list-inside space-y-1 text-base leading-relaxed"
            style={{ color: "var(--foreground)", opacity: 0.8 }}
          >
            <li>All processing runs on this device.</li>
            <li>No telemetry, no analytics, no account.</li>
            <li>Your library, your hardware, your rules.</li>
          </ul>
        </section>

        {/* Hardware acceleration */}
        <section className="space-y-3">
          <h2
            className="text-xl font-medium"
            style={{ color: "var(--foreground)" }}
          >
            Hardware acceleration
          </h2>
          <ul
            className="list-disc list-inside space-y-1 text-base leading-relaxed"
            style={{ color: "var(--foreground)", opacity: 0.8 }}
          >
            <li>macOS: Apple Silicon (MPS) and VideoToolbox.</li>
            <li>Windows: NVIDIA GPUs via CUDA + NVENC.</li>
            <li>Falls back to CPU when no GPU is available.</li>
          </ul>
        </section>

        {/* Open source */}
        <section className="space-y-3">
          <h2
            className="text-xl font-medium"
            style={{ color: "var(--foreground)" }}
          >
            Open source
          </h2>
          <p
            className="text-base leading-relaxed"
            style={{ color: "var(--foreground)", opacity: 0.8 }}
          >
            SovLens is built in the open. Contribute, file issues, or suggest
            features at our repo.
          </p>
          <a
            href="https://github.com/"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-opacity hover:opacity-80"
            style={{
              background: "var(--panel-bg)",
              border: "1px solid var(--panel-border)",
              color: "var(--foreground)",
            }}
          >
            <GithubIcon className="w-5 h-5" />
            <span>View on GitHub</span>
          </a>
        </section>

        {/* Divider */}
        <hr style={{ borderColor: "var(--panel-border)" }} />

        {/* Footer */}
        <p
          className="text-sm text-center"
          style={{ color: "var(--muted-text)" }}
        >
          Built by SovStac with{" "}
          <Heart
            className="inline-block w-4 h-4 align-middle"
            style={{ color: "var(--accent)", fill: "var(--accent)" }}
          />{" "}
          in Pakistan
        </p>
      </div>
    </div>
  );
}
