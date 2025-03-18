#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""OpenAI Bot Implementation.

This module implements a chatbot using OpenAI's GPT-4 model for natural language
processing. It includes:
- Real-time audio/video interaction through Daily
- Animated robot avatar
- Text-to-speech using ElevenLabs
- Support for both English and Spanish

The bot runs as part of a pipeline that processes audio/video frames and manages
the conversation flow.
"""

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from runner import configure

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.mem0 import Mem0MemoryService
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
load_dotenv(override=True)
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
)

try:
    from mem0 import MemoryClient
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use Mem0, you need to `pip install mem0ai`. Also, set the environment variable MEM0_API_KEY."
    )
    raise Exception(f"Missing module: {e}")


async def get_initial_greeting(memory_client: MemoryClient, user_id: str, agent_id: str, run_id: str) -> str:
    """Fetch all memories for the user and create a personalized greeting.
    
    Returns:
        A personalized greeting based on user memories
    """
    try:
        # Create filters based on available IDs
        id_pairs = [("user_id", user_id), ("agent_id", agent_id), ("run_id", run_id)]
        clauses = [{name: value} for name, value in id_pairs if value is not None]
        filters = {"AND": clauses} if clauses else {}
        
        # Get all memories for this user
        memories = memory_client.get_all(filters=filters, version="v2")

        if not memories or len(memories) == 0:
            return "Hello! It's nice to meet you. How can I help you today?"

        # Create a personalized greeting based on memories
        greeting = "Hello! It's great to see you again. "
        
        # Add some personalization based on memories (limit to 3 memories for brevity)
        if len(memories) > 0:
            greeting += "Based on our previous conversations, I remember: "
            for i, memory in enumerate(memories[:3], 1):
                memory_content = memory.get('memory', '')
                # Keep memory references brief
                if len(memory_content) > 100:
                    memory_content = memory_content[:97] + "..."
                greeting += f"{memory_content} "

            greeting += "How can I help you today?"

        logger.debug(f"Created personalized greeting from {len(memories)} memories")
        return greeting

    except Exception as e:
        logger.error(f"Error retrieving initial memories from Mem0: {e}")
        return "Hello! How can I help you today?"


async def main():
    """Main bot execution function.

    Sets up and runs the bot pipeline including:
    - Daily video transport
    - Speech-to-text and text-to-speech services
    - Language model integration
    - Mem0 memory service
    - RTVI event handling
    """
    # Note: You can pass the user_id as a parameter in API call
    USER_ID = "deshraj"
    async with aiohttp.ClientSession() as session:
        (room_url, token) = await configure(session)

        # Set up Daily transport with video/audio parameters
        transport = DailyTransport(
            room_url,
            token,
            "Chatbot",
            DailyParams(
                audio_out_enabled=True,
                camera_out_enabled=True,
                camera_out_width=1024,
                camera_out_height=576,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
                transcription_enabled=True,
                #
                # Spanish
                #
                # transcription_settings=DailyTranscriptionSettings(
                #     language="es",
                #     tier="nova",
                #     model="2-general"
                # )
            ),
        )

        # Initialize text-to-speech service
        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            #
            # English
            #
            voice_id="pNInz6obpgDQGcFmaJgB",
            #
            # Spanish
            #
            # model="eleven_multilingual_v2",
            # voice_id="gD1IexrzCvsXPHUuT0s3",
        )

        # Initialize Mem0 memory service
        memory = Mem0MemoryService(
            api_key=os.getenv("MEM0_API_KEY"),
            user_id=USER_ID,  # Unique identifier for the user
            # agent_id="life_coach_bot",  # Optional identifier for the agent
            # run_id="session_1", # Optional identifier for the run
            params=Mem0MemoryService.InputParams(
                search_limit=10,
                search_threshold=0.3,
                api_version="v2",
                system_prompt="Based on previous conversations, I recall: \n\n",
                add_as_system_message=True,
                position=1
            )
        )

        # Initialize LLM service
        llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")

        messages = [
            {
                "role": "system",
                "content": """You are a personal assistant. You can remember things about the person you are talking to.
                            Some Guidelines:
                            - Make sure your responses are friendly yet short and concise.
                            - If the user asks you to remember something, make sure to remember it.
                            - Greet the user by their name if you know about it.
                        """
            },
        ]

        # Set up conversation context and management
        # The context_aggregator will automatically collect conversation context
        context = OpenAILLMContext(messages)
        context_aggregator = llm.create_context_aggregator(context)

        #
        # RTVI events for Pipecat client UI
        #
        rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

        pipeline = Pipeline(
            [
                transport.input(),
                rtvi,
                context_aggregator.user(),
                memory,
                llm,
                tts,
                transport.output(),
                context_aggregator.assistant(),
            ]
        )

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[RTVIObserver(rtvi)],
        )

        @rtvi.event_handler("on_client_ready")
        async def on_client_ready(rtvi):
            await rtvi.set_bot_ready()

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await transport.capture_participant_transcription(participant["id"])
            
            # Get personalized greeting based on user memories. Can pass agent_id and run_id as per requirement of the application to manage short term memory or agent specific memory.
            greeting = await get_initial_greeting(memory_client=memory.memory_client, user_id=USER_ID, agent_id=None, run_id=None)
            
            # Add the greeting as an assistant message to start the conversation
            context.add_message({"role": "assistant", "content": greeting})
            
            # Queue the context frame to start the conversation
            await task.queue_frames([context_aggregator.user().get_context_frame()])

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            print(f"Participant left: {participant}")
            await task.cancel()

        runner = PipelineRunner()

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
