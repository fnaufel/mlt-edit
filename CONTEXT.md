# Video Edit Planning

This context describes decisions captured during an OBS recording and the resulting video edit.

## Language

**Source recording**:
The single video file whose footage is selected for an edit.

**Audio source**:
The sound selected for the finished edit, either from a stream in the source recording or from a separate audio file aligned to the source recording's timeline. It follows the same kept segments and joins as the video.
_Avoid_: Soundtrack

**Edit boundary**:
A marker that closes and classifies the interval since the previous edit boundary, or since the start of the source recording. It can also specify how a kept interval joins the next kept interval.

**Segment**:
An interval of the source recording classified as kept or discarded by its closing edit boundary. Segments remain in source order in the edit plan, including those classified as discarded, and cover the classified portion of the recording without gaps or overlaps. Their boundaries can be corrected or split by hand.

**Join**:
The connection between a kept segment and the next kept segment in the finished video, even when discarded footage separates them in the source recording. A join is a cut or a transition.

**Transition**:
A join that overlaps the end of one kept segment with the start of the next, dissolving the video and crossfading the audio. Only footage within the kept segments participates.

**Annotation**:
A point or range of interest in the source recording that does not close or classify a segment. Its position remains relative to the source recording even when that footage is discarded from the finished video, and a range can remain incomplete when its end was not captured.

**Ignored marker**:
A capture marker explicitly designated to have no effect on the edit plan.

**Edit plan**:
An independently editable description of which parts of the source recording survive and how adjacent parts are joined. It can be corrected by hand and rendered without consulting the original marker log.
_Avoid_: Generated report
