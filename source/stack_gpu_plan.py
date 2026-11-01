"""Keep export treatment filters, replace preview alpha mixing with an atlas."""
from program_edits import grade_at
from realtime_grade import grade_uniforms
from stack_gpu import atlas_layout


def _chains(graph):
    """FFmpeg chain separators exclude escaped/quoted filename semicolons."""
    start=0;quote=False;escaped=False
    for i,char in enumerate(graph):
        if escaped: escaped=False;continue
        if char=='\\': escaped=True;continue
        if char=="'": quote=not quote
        elif char==';' and not quote:
            yield graph[start:i];start=i+1
    yield graph[start:]


def layer_atlas_command(command, fps, segment, project, width, height, ass_path=None):
    """Return original CPU graph for unsupported final effects, never omit them."""
    local = None if getattr(project,'multicam_outer_order',()) else grade_at(project,segment.start)
    final = [local, project.global_grade]
    if (not segment.layers or segment.camera == -1 or ass_path
            or any(g is not None and g.enabled and g.lut_path for g in final)):
        return command, None
    values, notices = grade_uniforms(final)
    if notices: return command, None
    layout=atlas_layout(len(segment.layers),width,height)
    command=list(command)
    graph_index=command.index('-filter_complex')+1
    parts=[part for part in _chains(command[graph_index])
           if not part.startswith('color=') and not part.startswith('[stack')
           and not part.startswith('[project_picture]')]
    n=layout['count']
    if n==1:
        tail='[layer0]'
    else:
        positions='|'.join(f'{i%layout["columns"]*width}_{i//layout["columns"]*height}' for i in range(n))
        # Unused cells need to exist so every atlas row has the same width.
        # xstack's output can end short when the last row is incomplete.
        tail=''.join(f'[layer{i}]' for i in range(n))
        tail+=f'xstack=inputs={n}:layout={positions}:fill=black@0,'
        tail+=f'pad={width*layout["columns"]}:{height*layout["rows"]}:0:0:color=black@0,'
    parts.append(tail+f'fps=fps={fps:.12g}:start_time=0,format=rgba[picture]')
    command[graph_index]=';'.join(parts)
    layout['grades']=list(values.values())
    return command, layout
