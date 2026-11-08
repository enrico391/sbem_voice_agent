"""Local microphone and speaker transport with the original CLI defaults."""

from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams


def create_transport(args) -> LocalAudioTransport:
    return LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            input_device_index=getattr(args, "input_device", None),
            output_device_index=getattr(args, "output_device", None),
            audio_in_sample_rate=getattr(args, "input_sample_rate", 16000),
            audio_out_sample_rate=getattr(args, "output_sample_rate", 48000),
        )
    )
