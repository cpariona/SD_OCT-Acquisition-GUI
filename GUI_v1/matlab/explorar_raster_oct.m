function raster = explorar_raster_oct(entrada, varargin)
%EXPLORAR_RASTER_OCT Reconstruye (si hace falta) y abre un visor interactivo
%   de un raster OCT/OCE: B-scan actual a la izquierda, enface (promedio en
%   profundidad) a la derecha, con navegacion por slider, botones, flechas
%   del teclado o clic directo sobre el enface.
%
%   RASTER = EXPLORAR_RASTER_OCT() abre un selector de archivo .bin en la
%   carpeta data del proyecto, reconstruye el raster y abre el visor.
%
%   RASTER = EXPLORAR_RASTER_OCT(ARCHIVO) reconstruye ARCHIVO (ruta .bin).
%
%   RASTER = EXPLORAR_RASTER_OCT(ARCHIVO, 'Nombre', Valor, ...) reenvia las
%   opciones de nombre-valor a RECONSTRUIR_RASTER_OCT (FFTSize, DepthBins,
%   RemoveDC, etc.).
%
%   RASTER = EXPLORAR_RASTER_OCT(RASTER_YA_CALCULADO) reabre el visor sobre
%   una estructura ya devuelta por RECONSTRUIR_RASTER_OCT o por una llamada
%   previa a EXPLORAR_RASTER_OCT, sin recalcular.
%
%   Controles:
%     - Arrastrar el slider inferior, o usar los botones "< Anterior" /
%       "Siguiente >", o las flechas izquierda/derecha del teclado.
%     - Clic sobre el enface: salta al B-scan mas cercano a esa posicion Y.
%     - Cajas "Clim min/max (dB)": ajustan manualmente los limites de la
%       escala de color del B-scan; "Auto" recalcula el rango por percentiles.
%
%   Ejemplo:
%       explorar_raster_oct('data/Alevin_al_aire.bin');
%       explorar_raster_oct('data/Alevin_sumergido.bin', 'DepthBins', 1024);

    rutaFunciones = fileparts(mfilename('fullpath'));
    if isempty(which('reconstruir_raster_oct'))
        addpath(rutaFunciones);
    end

    if nargin < 1 || isempty(entrada)
        carpetaDatos = fullfile(rutaFunciones, '..', 'data');
        [nombre, carpeta] = uigetfile(fullfile(carpetaDatos, '*.bin'), 'Seleccione archivo OCT/OCE');
        if isequal(nombre, 0)
            error('ExplorarRaster:Cancelado', 'No se selecciono un archivo.');
        end
        entrada = fullfile(carpeta, nombre);
    end

    if (ischar(entrada) || isstring(entrada))
        fprintf('Reconstruyendo raster de %s...\n', char(entrada));
        raster = reconstruir_raster_oct(char(entrada), varargin{:});
    elseif isstruct(entrada) && isfield(entrada, 'volumen_db')
        raster = entrada;
    else
        error('ExplorarRaster:Entrada', ...
            'ENTRADA debe ser una ruta .bin o una estructura de RECONSTRUIR_RASTER_OCT.');
    end

    [~, nAlines, nBscans] = size(raster.volumen_db); %#ok<ASGLU>
    indice = max(1, min(nBscans, round(nBscans / 2)));

    submuestra = raster.volumen_db(1:max(1, floor(end/256)):end, ...
                                    1:max(1, floor(nAlines/256)):end, ...
                                    1:max(1, floor(nBscans/64)):end);
    climsAuto = prctile(single(submuestra(:)), [2 99.5]);
    if climsAuto(2) <= climsAuto(1)
        climsAuto(2) = climsAuto(1) + 1;
    end
    clims = climsAuto;

    [~, nombreArchivo, ext] = fileparts(char(string(raster.archivo)));
    fig = figure('Name', sprintf('Explorador de raster OCT - %s%s', nombreArchivo, ext), ...
        'NumberTitle', 'off', 'Color', 'w', 'Position', [80 80 1300 700], ...
        'KeyPressFcn', @alPresionarTecla);

    axBscan = axes('Parent', fig, 'Units', 'normalized', 'Position', [0.06 0.22 0.53 0.70]);
    imgBscan = imagesc(axBscan, raster.eje_x_mm, raster.eje_z_bin, ...
        raster.volumen_db(:, :, indice), clims);
    colormap(axBscan, gray);
    % Se deja la orientacion por defecto de imagesc (bin 1, el mas cercano
    % a DC/superficie, arriba), que es la convencion habitual de un B-scan.
    xlabel(axBscan, 'Posicion lateral (mm)');
    ylabel(axBscan, 'Profundidad (bin FFT)');
    tituloBscan = title(axBscan, '');
    barraColor = colorbar(axBscan);
    barraColor.Label.String = 'Intensidad (dB)';

    axEnface = axes('Parent', fig, 'Units', 'normalized', 'Position', [0.66 0.22 0.28 0.70]);
    imgEnface = imagesc(axEnface, raster.eje_x_mm, raster.eje_y_mm, raster.enface); %#ok<NASGU>
    colormap(axEnface, gray);
    axis(axEnface, 'image');
    xlabel(axEnface, 'X (mm)');
    ylabel(axEnface, 'Y (mm, posicion de B-scan)');
    title(axEnface, 'Enface (promedio en profundidad, dB)');
    hold(axEnface, 'on');
    lineaIndicadora = plot(axEnface, raster.eje_x_mm([1 end]), ...
        [raster.eje_y_mm(indice) raster.eje_y_mm(indice)], 'y-', 'LineWidth', 1.5);
    set(imgEnface, 'ButtonDownFcn', @alHacerClicEnface);
    set(axEnface, 'ButtonDownFcn', @alHacerClicEnface);

    slider = uicontrol(fig, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [0.06 0.115 0.53 0.045], 'Min', 1, 'Max', nBscans, 'Value', indice, ...
        'SliderStep', [1 / max(1, nBscans - 1), max(1, round(nBscans / 20)) / max(1, nBscans - 1)], ...
        'Callback', @alMoverSlider);

    textoIndice = uicontrol(fig, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.60 0.110 0.06 0.05], 'BackgroundColor', 'w', 'FontSize', 10);

    uicontrol(fig, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.67 0.110 0.11 0.05], 'String', '< Anterior', 'Callback', @(~, ~) cambiarIndice(-1));
    uicontrol(fig, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.79 0.110 0.11 0.05], 'String', 'Siguiente >', 'Callback', @(~, ~) cambiarIndice(1));

    uicontrol(fig, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.06 0.03 0.13 0.045], 'String', 'Clim B-scan (dB):', ...
        'BackgroundColor', 'w', 'FontSize', 10, 'HorizontalAlignment', 'left');
    uicontrol(fig, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.195 0.035 0.03 0.04], 'String', 'min', 'BackgroundColor', 'w');
    editClimMin = uicontrol(fig, 'Style', 'edit', 'Units', 'normalized', ...
        'Position', [0.225 0.035 0.08 0.045], 'String', sprintf('%.1f', clims(1)), ...
        'Callback', @alEditarClim);
    uicontrol(fig, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.315 0.035 0.03 0.04], 'String', 'max', 'BackgroundColor', 'w');
    editClimMax = uicontrol(fig, 'Style', 'edit', 'Units', 'normalized', ...
        'Position', [0.345 0.035 0.08 0.045], 'String', sprintf('%.1f', clims(2)), ...
        'Callback', @alEditarClim);
    sliderClimMin = uicontrol(fig, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [0.44 0.045 0.15 0.03], 'Min', 0, 'Max', 120, 'Value', clims(1), ...
        'Callback', @alMoverSliderClim);
    sliderClimMax = uicontrol(fig, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [0.44 0.010 0.15 0.03], 'Min', 0, 'Max', 120, 'Value', clims(2), ...
        'Callback', @alMoverSliderClim);
    uicontrol(fig, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.60 0.03 0.09 0.045], 'String', 'Auto', 'Callback', @(~, ~) restablecerClimAuto());

    actualizarVista();
    actualizarControlesClim();

    function cambiarIndice(delta)
        indice = max(1, min(nBscans, indice + delta));
        actualizarVista();
    end

    function alMoverSlider(origen, ~)
        indice = max(1, min(nBscans, round(origen.Value)));
        actualizarVista();
    end

    function alPresionarTecla(~, evento)
        switch evento.Key
            case 'leftarrow'
                cambiarIndice(-1);
            case 'rightarrow'
                cambiarIndice(1);
            case 'pageup'
                cambiarIndice(10);
            case 'pagedown'
                cambiarIndice(-10);
        end
    end

    function alHacerClicEnface(~, ~)
        punto = get(axEnface, 'CurrentPoint');
        yClic = punto(1, 2);
        [~, indice] = min(abs(raster.eje_y_mm - yClic));
        actualizarVista();
    end

    function alEditarClim(~, ~)
        nuevoMin = str2double(get(editClimMin, 'String'));
        nuevoMax = str2double(get(editClimMax, 'String'));
        if isnan(nuevoMin) || isnan(nuevoMax) || nuevoMax <= nuevoMin
            actualizarControlesClim(); % restaura los valores validos previos
            return;
        end
        clims = [nuevoMin, nuevoMax];
        aplicarClim();
    end

    function alMoverSliderClim(~, ~)
        nuevoMin = get(sliderClimMin, 'Value');
        nuevoMax = get(sliderClimMax, 'Value');
        if nuevoMax <= nuevoMin
            actualizarControlesClim();
            return;
        end
        clims = [nuevoMin, nuevoMax];
        aplicarClim();
    end

    function restablecerClimAuto()
        clims = climsAuto;
        aplicarClim();
    end

    function aplicarClim()
        caxis(axBscan, clims); %#ok<CAXIS>
        actualizarControlesClim();
    end

    function actualizarControlesClim()
        set(editClimMin, 'String', sprintf('%.1f', clims(1)));
        set(editClimMax, 'String', sprintf('%.1f', clims(2)));
        set(sliderClimMin, 'Value', min(max(clims(1), get(sliderClimMin, 'Min')), get(sliderClimMin, 'Max')));
        set(sliderClimMax, 'Value', min(max(clims(2), get(sliderClimMax, 'Min')), get(sliderClimMax, 'Max')));
    end

    function actualizarVista()
        set(imgBscan, 'CData', raster.volumen_db(:, :, indice));
        set(tituloBscan, 'String', sprintf('B-scan %d / %d  (y = %.3f mm)', indice, nBscans, raster.eje_y_mm(indice)));
        set(lineaIndicadora, 'YData', [raster.eje_y_mm(indice) raster.eje_y_mm(indice)]);
        set(slider, 'Value', indice);
        set(textoIndice, 'String', sprintf('%d/%d', indice, nBscans));
        drawnow limitrate;
    end
end
