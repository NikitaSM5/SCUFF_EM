/* Three.js r128 is bundled for the Chromium version supplied with Qt 5.15.2. */
(function () {
  'use strict';
  const error = document.getElementById('error');
  try {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10000);
    camera.up.set(0, 0, 1);
    const renderer = new THREE.WebGLRenderer({antialias:true, preserveDrawingBuffer:true});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    document.body.prepend(renderer.domElement);
    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new THREE.AmbientLight(0xffffff, .75));
    const light = new THREE.DirectionalLight(0xffffff, .65);
    light.position.set(3, -4, 7); scene.add(light);
    let content = new THREE.Group(); scene.add(content);
    let span = 40, lastGeometry = '', fitted = false;
    const colors = [new THREE.Color('#166fc0'),new THREE.Color('#23be70'),new THREE.Color('#ffee33'),new THREE.Color('#ff3822')];
    function gainColor(t) {
      t = Math.max(0, Math.min(.999999, t))*3;
      return colors[Math.floor(t)].clone().lerp(colors[Math.floor(t)+1], t%1);
    }
    function dispose() {
      content.traverse(o => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          if (o.material.map) o.material.map.dispose();
          o.material.dispose();
        }
      });
      scene.remove(content); content = new THREE.Group(); scene.add(content);
    }
    function box(x,y,z,w,d,h,color,opacity) {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(w,d,h), new THREE.MeshPhongMaterial({color,transparent:opacity<1,opacity,depthWrite:opacity===1}));
      mesh.position.set(x,y,z); content.add(mesh); return mesh;
    }
    function label(text, x, y, z, color) {
      const c = document.createElement('canvas'); c.width=128; c.height=64;
      const ctx=c.getContext('2d'); ctx.fillStyle=color; ctx.font='36px sans-serif'; ctx.textAlign='center'; ctx.fillText(text,64,44);
      const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),depthTest:false}));
      sprite.position.set(x,y,z); sprite.scale.set(span*.11,span*.055,1); content.add(sprite);
    }
    window.fitView = function () {
      controls.target.set(0,0,span*.12);
      const distance=span*2.25*Math.max(1,1/camera.aspect);
      camera.position.set(distance*.8,-distance,distance*.8);
      camera.near=span/1000; camera.far=span*100;
      camera.updateProjectionMatrix(); controls.update(); fitted=true;
    };
    function resize() {
      const w=window.innerWidth, h=window.innerHeight;
      if (!w || !h) return;
      renderer.setSize(w,h); camera.aspect=w/h; camera.updateProjectionMatrix();
    }
    new ResizeObserver(resize).observe(document.body);
    window.setScene = function (data) {
      dispose(); scene.background=new THREE.Color(data.background || '#ffffff');
      document.body.style.background=data.background || '#ffffff';
      document.body.style.color=data.foreground || '#222222';
      const p=data.project, n=p.cells.length, a=p.cell_size_mm, h=p.thickness_mm;
      span=Math.max(n*a,h*3); const extent=n*a;
      const signature=JSON.stringify([p.cells,a,h,p.feed_cell,data.feed_model]);
      if(data.show_antenna) {
        box(0,0,-h/2,extent,extent,h,'#54a491',.20);
        box(0,0,-h,extent,extent,Math.max(span*.002,.01),'#b4b9bf',.75);
        p.cells.forEach((row,r)=>row.forEach((metal,c)=>{
          if(metal) box((c+.5-n/2)*a,(n/2-r-.5)*a,0,a,a,span*.002,'#da9639',1);
        }));
        if(p.feed_cell) {
          const r=p.feed_cell[0],c=p.feed_cell[1],x=(c+.5-n/2)*a,y=(n/2-r-.5)*a;
          if(data.feed_model==='finite_radius_wire') {
            const radius=((p.feko||{}).feed_diameter_mm||.5)/2;
            const pin=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,h,20),new THREE.MeshPhongMaterial({color:'#ba6b25'}));
            pin.rotation.x=Math.PI/2; pin.position.set(x,y,-h/2); content.add(pin);
          } else {
            // The visible stripe marks an ideal zero-width voltage discontinuity.
            box(x,y,span*.002,a*.025,a,span*.002,'#ce3041',1);
            const arrow=new THREE.ArrowHelper(new THREE.Vector3(1,0,0),new THREE.Vector3(x-a*.25,y,span*.015),a*.5,0xce3041,a*.12,a*.08);
            content.add(arrow);
          }
        }
      }
      const axis=new THREE.AxesHelper(span*.64); content.add(axis);
      label('X',span*.70,0,0,'#ce4336'); label('Y',0,span*.70,0,'#24833d'); label('Z',0,0,span*.70,'#2676ba');
      const legend=document.getElementById('legend'); legend.style.display='none';
      if(data.pattern && data.show_pattern) {
        const points=data.pattern.points;
        const theta=Array.from(new Set(points.map(v=>v[0]))).sort((x,y)=>x-y);
        const phi=Array.from(new Set(points.map(v=>v[1]))).sort((x,y)=>x-y);
        const values=new Map(points.map(v=>[v[0]+','+v[1],v[2]]));
        const peak=Math.max.apply(null,points.map(v=>v[2]));
        const positions=[], vertexColors=[], indices=[];
        phi.forEach((ph,j)=>theta.forEach((th,i)=>{
          const gain=values.get(th+','+ph);
          if(gain===undefined) throw new Error('Неполная угловая сетка ДН');
          const relative=gain-peak;
          const radius=span*(data.scale===1 ? Math.max(0,(relative+40)/40) : Math.pow(10,relative/10));
          const t=th*Math.PI/180, f=ph*Math.PI/180;
          positions.push(radius*Math.sin(t)*Math.cos(f),radius*Math.sin(t)*Math.sin(f),radius*Math.cos(t));
          const col=gainColor((relative+40)/40); vertexColors.push(col.r,col.g,col.b);
          if(i<theta.length-1 && j<phi.length-1) {
            const k=j*theta.length+i, l=k+theta.length;
            indices.push(k,l,k+1,k+1,l,l+1);
          }
        }));
        const geometry=new THREE.BufferGeometry();
        geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
        geometry.setAttribute('color',new THREE.Float32BufferAttribute(vertexColors,3));
        geometry.setIndex(indices); geometry.computeVertexNormals();
        content.add(new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide,transparent:true,opacity:.78,depthWrite:false})));
        legend.style.display='block'; document.getElementById('high').textContent=peak.toFixed(2);
        document.getElementById('low').textContent=(peak-40).toFixed(2);
      }
      document.getElementById('units').textContent=data.show_antenna ? 'Антенна: мм · Земля: ∞' : '';
      resize(); if(!fitted || lastGeometry!==signature) window.fitView(); lastGeometry=signature;
      window.sceneStats={objects:content.children.length,patternPoints:data.pattern ? data.pattern.points.length : 0,feedModel:data.feed_model||'planar_delta_gap_voltage'};
    };
    window.viewerInfo=function(){ return {webgl:!!renderer.getContext(), calls:renderer.info.render.calls, camera:camera.position.toArray(),...window.sceneStats}; };
    function animate(){ requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); }
    resize(); window.fitView(); animate(); window.viewerReady=true;
  } catch(e) { error.textContent='3D: '+e.message; error.style.display='block'; window.viewerError=e.message; }
})();
