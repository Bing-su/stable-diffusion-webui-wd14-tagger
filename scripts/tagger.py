import os
import json
import gradio as gr

from typing import List, Dict
from pathlib import Path
from glob import glob
from PIL import Image, ImageFile, UnidentifiedImageError

from webui import wrap_gradio_gpu_call
from modules import shared, scripts, script_callbacks, ui
from modules import generation_parameters_copypaste as parameters_copypaste

from tagger import format
from tagger.preset import Preset
from tagger.interrogator import Interrogator, DeepDanbooruInterrogator, WaifuDiffusionInterrogator
from tagger.utils import split_str

script_dir = Path(scripts.basedir())
preset = Preset(script_dir.joinpath('presets'))
interrogators: Dict[str, Interrogator] = {}

# if you do not initialize the Image object
# Image.registered_extensions() returns only PNG
Image.init()

# PIL spits errors when loading a truncated image by default
# https://pillow.readthedocs.io/en/stable/reference/ImageFile.html#PIL.ImageFile.LOAD_TRUNCATED_IMAGES
ImageFile.LOAD_TRUNCATED_IMAGES = True


def refresh_interrogators() -> List[str]:
    global interrogators
    interrogators = {}

    # load waifu diffusion 1.4 tagger models
    interrogators['wd14-vit'] = WaifuDiffusionInterrogator()
    interrogators['wd14-convnext'] = WaifuDiffusionInterrogator(
        repo='SmilingWolf/wd-v1-4-convnext-tagger'
    )

    # load deepdanbooru project
    os.makedirs(
        shared.cmd_opts.deepdanbooru_projects_path,
        exist_ok=True
    )

    for path in os.scandir(shared.cmd_opts.deepdanbooru_projects_path):
        if not path.is_dir():
            continue

        if not Path(path, 'project.json').is_file():
            continue

        interrogators[path.name] = DeepDanbooruInterrogator(path)

    return sorted(interrogators.keys())


def on_ui_tabs():
    with gr.Blocks(analytics_enabled=False) as tagger_interface:
        with gr.Row().style(equal_height=False):
            with gr.Column(variant='panel'):

                # input components
                with gr.Tabs():
                    with gr.TabItem(label='Single process'):
                        image = gr.Image(
                            label='Source',
                            source='upload',
                            interactive=True,
                            type="pil"
                        )

                    with gr.TabItem(label='Batch from directory'):
                        batch_input_glob = preset.component(
                            gr.Textbox,
                            label='Input directory',
                            placeholder='/path/to/images or /path/to/images/**/*'
                        )
                        batch_input_recursive = preset.component(
                            gr.Checkbox,
                            label='Use recursive with glob pattern'
                        )

                        batch_output_dir = preset.component(
                            gr.Textbox,
                            label='Output directory',
                            placeholder='Leave blank to save images to the same path.'
                        )

                        batch_output_filename_format = preset.component(
                            gr.Textbox,
                            label='Output filename format',
                            placeholder='Leave blank to use same filename as original.',
                            value='[name].[output_extension]'
                        )

                        import hashlib
                        with gr.Accordion(
                            label='Output filename formats',
                            open=False
                        ):
                            gr.Markdown(
                                value=f'''
                                ### Related to original file
                                - `[name]`: Original filename without extension
                                - `[extension]`: Original extension
                                - `[hash:<algorithms>]`: Original extension
                                    Available algorithms: `{', '.join(hashlib.algorithms_available)}`

                                ### Related to output file
                                - `[output_extension]`: Output extension (has no dot)

                                ## Examples
                                ### Original filename without extension
                                `[name].[output_extension]`

                                ### Original file's hash (good for deleting duplication)
                                `[hash:sha1].[output_extension]`
                                '''
                            )

                        batch_output_action_on_conflict = preset.component(
                            gr.Dropdown,
                            label='Action on exiting caption',
                            value='ignore',
                            choices=[
                                'ignore',
                                'copy',
                                'append',
                                'prepend'
                            ]
                        )

                        batch_output_save_json = preset.component(
                            gr.Checkbox,
                            label='Save with JSON'
                        )

                submit = gr.Button(
                    value='Interrogate',
                    variant='primary'
                )

                info = gr.HTML()

                # preset selector
                with gr.Row(variant='compact'):
                    available_presets = preset.list()
                    selected_preset = gr.Dropdown(
                        label='Preset',
                        choices=available_presets,
                        value=available_presets[0]
                    )

                    save_preset_button = gr.Button(
                        value=ui.save_style_symbol
                    )

                    ui.create_refresh_button(
                        selected_preset,
                        lambda: None,
                        lambda: {'choices': preset.list()},
                        'refresh_preset'
                    )

                # option components

                # interrogator selector
                with gr.Row(variant='compact'):
                    interrogator_names = refresh_interrogators()
                    interrogator = preset.component(
                        gr.Dropdown,
                        label='Interrogator',
                        choices=interrogator_names,
                        value=(
                            None
                            if len(interrogator_names) < 1 else
                            interrogator_names[-1]
                        )
                    )

                    ui.create_refresh_button(
                        interrogator,
                        lambda: None,
                        lambda: {'choices': refresh_interrogators()},
                        'refresh_interrogator'
                    )

                threshold = preset.component(
                    gr.Slider,
                    label='Threshold',
                    minimum=0,
                    maximum=1,
                    value=0.35
                )

                additional_tags = preset.component(
                    gr.Textbox,
                    label='Additional tags (split by comma)',
                    elem_id='additioanl-tags'
                )

                exclude_tags = preset.component(
                    gr.Textbox,
                    label='Exclude tags (split by comma)',
                    elem_id='exclude-tags'
                )

                sort_by_alphabetical_order = preset.component(
                    gr.Checkbox,
                    label='Sort by alphabetical order',
                )
                add_confident_as_weight = preset.component(
                    gr.Checkbox,
                    label='Include confident of tags matches in results'
                )
                replace_underscore = preset.component(
                    gr.Checkbox,
                    label='Use spaces instead of underscore',
                    value=True
                )
                replace_underscore_excludes = preset.component(
                    gr.Textbox,
                    label='Excudes (split by comma)',
                    # kaomoji from WD 1.4 tagger csv. thanks, Meow-San#5400!
                    value='0_0, (o)_(o), +_+, +_-, ._., <o>_<o>, <|>_<|>, =_=, >_<, 3_3, 6_9, >_o, @_@, ^_^, o_o, u_u, x_x, |_|, ||_||'
                )
                escape_tag = preset.component(
                    gr.Checkbox,
                    label='Escape brackets',
                )

            # output components
            with gr.Column(variant='panel'):
                tags = gr.Textbox(
                    label='Tags',
                    placeholder='Found tags',
                    interactive=False
                )

                with gr.Row():
                    parameters_copypaste.bind_buttons(
                        parameters_copypaste.create_buttons(
                            ["txt2img", "img2img"],
                        ),
                        None,
                        tags
                    )

                rating_confidents = gr.Label(
                    label='Rating confidents',
                    elem_id='rating-confidents'
                )
                tag_confidents = gr.Label(
                    label='Tag confidents',
                    elem_id='tag-confidents'
                )

        selected_preset.change(
            fn=preset.apply,
            inputs=[selected_preset],
            outputs=[*preset.components, info]
        )

        save_preset_button.click(
            fn=preset.save,
            inputs=[selected_preset, *preset.components],  # values only
            outputs=[info]
        )

        def give_me_the_tags(
            image: Image,
            batch_input_glob: str,
            batch_input_recursive: bool,
            batch_output_dir: str,
            batch_output_filename_format: str,
            batch_output_action_on_conflict: str,
            batch_output_save_json: bool,

            interrogator: str,
            threshold: float,
            additional_tags: str,
            exclude_tags: str,
            sort_by_alphabetical_order: bool,
            add_confident_as_weight: bool,
            replace_underscore: bool,
            replace_underscore_excludes: str,
            escape_tag: bool
        ):
            if interrogator not in interrogators:
                return ['', None, None, f"'{interrogator}' is not a valid interrogator"]

            interrogator: Interrogator = interrogators[interrogator]

            postprocess_opts = (
                threshold,
                split_str(additional_tags),
                split_str(exclude_tags),
                sort_by_alphabetical_order,
                add_confident_as_weight,
                replace_underscore,
                split_str(replace_underscore_excludes),
                escape_tag
            )

            # single process
            if image is not None:
                ratings, tags = interrogator.interrogate(image)
                processed_tags = Interrogator.postprocess_tags(
                    tags,
                    *postprocess_opts
                )

                return [
                    ', '.join(processed_tags),
                    ratings,
                    tags,
                    ''
                ]

            # batch process
            batch_input_glob = batch_input_glob.strip()
            batch_output_dir = batch_output_dir.strip()
            batch_output_filename_format = batch_output_filename_format.strip()

            if batch_input_glob != '':
                # if there is no glob pattern, insert it automatically
                if not batch_input_glob.endswith('*'):
                    if not batch_input_glob.endswith('/'):
                        batch_input_glob += '/'
                    batch_input_glob += '*'

                # get root directory of input glob pattern
                base_dir = batch_input_glob.replace('?', '*')
                base_dir = base_dir.split('/*').pop(0)

                # check the input directory path
                if not os.path.isdir(base_dir):
                    return ['', None, None, 'input path is not a directory']

                # this line is moved here because some reason
                # PIL.Image.registered_extensions() returns only PNG if you call too early
                supported_extensions = [
                    e
                    for e, f in Image.registered_extensions().items()
                    if f in Image.OPEN
                ]

                paths = [
                    Path(p)
                    for p in glob(batch_input_glob, recursive=batch_input_recursive)
                    if '.' + p.split('.').pop().lower() in supported_extensions
                ]

                print(f'found {len(paths)} image(s)')

                for path in paths:
                    try:
                        image = Image.open(path)
                    except UnidentifiedImageError:
                        # just in case, user has mysterious file...
                        print(f'${path} is not supported image type')
                        continue

                    # guess the output path
                    base_dir_last = Path(base_dir).parts[-1]
                    base_dir_last_idx = path.parts.index(base_dir_last)
                    output_dir = Path(batch_output_dir) if batch_output_dir else Path(base_dir)
                    output_dir = output_dir.joinpath(*path.parts[base_dir_last_idx + 1:]).parent

                    output_dir.mkdir(0o777, True, True)

                    # format output filename
                    format_info = format.Info(path, 'txt')

                    try:
                        formatted_output_filename = format.pattern.sub(
                            lambda m: format.format(m, format_info),
                            batch_output_filename_format
                        )
                    except (TypeError, ValueError) as error:
                        return ['', None, None, str(error)]

                    output_path = output_dir.joinpath(
                        formatted_output_filename
                    )

                    output = []

                    if output_path.is_file():
                        output.append(output_path.read_text())

                        if batch_output_action_on_conflict == 'ignore':
                            print(f'skipping {path}')
                            continue

                    ratings, tags = interrogator.interrogate(image)
                    processed_tags = Interrogator.postprocess_tags(
                        tags,
                        *postprocess_opts
                    )

                    # TODO: switch for less print
                    print(
                        f'found {len(processed_tags)} tags out of {len(tags)} from {path}'
                    )

                    plain_tags = ', '.join(processed_tags)

                    if batch_output_action_on_conflict == 'copy':
                        output = [plain_tags]
                    elif batch_output_action_on_conflict == 'prepend':
                        output.insert(0, plain_tags)
                    else:
                        output.append(plain_tags)

                    output_path.write_text(' '.join(output))

                    if batch_output_save_json:
                        output_path.with_suffix('.json').write_text(
                            json.dumps([ratings, tags])
                        )

                print('all done :)')

            return ['', None, None, '']

        # register events
        for func in [image.change, submit.click]:
            func(
                fn=wrap_gradio_gpu_call(give_me_the_tags),
                inputs=[
                    # single process
                    image,

                    # batch process
                    batch_input_glob,
                    batch_input_recursive,
                    batch_output_dir,
                    batch_output_filename_format,
                    batch_output_action_on_conflict,
                    batch_output_save_json,

                    # options
                    interrogator,
                    threshold,
                    additional_tags,
                    exclude_tags,
                    sort_by_alphabetical_order,
                    add_confident_as_weight,
                    replace_underscore,
                    replace_underscore_excludes,
                    escape_tag
                ],
                outputs=[
                    tags,
                    rating_confidents,
                    tag_confidents,

                    # contains execution time, memory usage and other stuffs...
                    # generated from modules.ui.wrap_gradio_call
                    info
                ]
            )

    return [(tagger_interface, "Tagger", "tagger")]


script_callbacks.on_ui_tabs(on_ui_tabs)
