# stock-selection-app

Testing whether Reddit chatter can identify stocks *before* they rise — using
Reddit posts and comments as the only input.

**Current status:** scope agreed, implementation not yet started.

📄 **[docs/SCOPE.md](docs/SCOPE.md)** — the agreed plan: data sources, signals,
noise handling, and the safeguards against fooling ourselves.

## The short version

The project turns on one question, answered before any strategy is built:
**does Reddit chatter come before price moves, or after?** People usually post
about a stock because it already jumped. If that is all that is happening, there
is no strategy here — and we want to know that cheaply and early.

Two datasets are compared head to head: WSB's daily "what are your moves tomorrow"
threads, and broad post capture across eight investing subreddits.

All data from 2024 onward is sealed as a final test and touched exactly once.
