When scanning a local TV media source, the scanner fails to import a large number of otherwise valid episodes because the media/metadata parser strictly requires fields that are not always present. This causes entire episodes or even complete shows to be skipped and marked as failed.

The failures manifest in several ways that need to be addressed:

1. **ffprobe output parsing fails on codec-less subtitle streams:** When an ffprobe output includes a subtitle stream (like VobSub tracks) that lacks a `codec_name`, the parsing fails with `Invalid input: expected string, received undefined → at streams.[index].codec_name`. This rejects the entire probe and marks the episode as failed. The `codec_name` should be made optional in the ffprobe schema. When constructing `FfprobeStreamDetails`, if `codec_name` is missing, it should be mapped to `'unknown'`.

2. **Confusing probe schema validation errors:** When an ffprobe result fails schema validation, the scanner currently logs `Filter was not a match`. This error is unclear and should be updated to log something like `ffprobe output for <path> did not match the expected schema` to better indicate the actual issue.

3. **Failing to parse malformed episode NFO files:** If an episode `.nfo` file is malformed and fails to parse, or if it lacks an `<episodedetails>` entry matching the filename's episode number (e.g., throwing `Invalid input: expected array, received undefined → at episodedetails`), the scanner currently skips the whole episode. Instead of throwing a fatal error, it should log a warning and fall back to using filename-derived metadata, handling it as if there were no NFO file.

4. **Failing to parse malformed show NFO files:** Similarly, if a `tvshow.nfo` fails to parse, the scanner currently skips the entire show folder. It should instead catch the error, log a warning, and gracefully fall back to using `FallbackMetadataService` for the show's metadata.

Please fix these issues so that bad NFOs or missing subtitle codecs result in graceful fallbacks rather than fatal scanning failures.