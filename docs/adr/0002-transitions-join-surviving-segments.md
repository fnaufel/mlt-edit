# Join the next surviving segment

A transition marker describes the outgoing join of the kept segment it closes. The join applies to the next kept segment in the finished video even when discarded footage lies between them in the source recording. The transition overlaps footage only from those two kept segments, shortening the finished video by its duration. This follows what the viewer will see and lets recording-time decisions survive later deletions; attaching the transition only to the next source interval would lose it whenever that interval is discarded.
