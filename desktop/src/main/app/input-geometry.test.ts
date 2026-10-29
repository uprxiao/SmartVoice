import { describe, expect, it, vi } from 'vitest';
import { inputGeometryToDip } from '../../../native/input-geometry';
describe('physical Windows bounds become DIP exactly once', () => {
  it.each([1,1.5,2])('preserves negative screen placement at scale %s', scale => {
    const rect = {x:-1800,y:300,width:3,height:24};
    const convert=vi.fn((r:typeof rect)=>({x:r.x/scale,y:r.y/scale,width:r.width/scale,height:r.height/scale}));
    const result=inputGeometryToDip({available:true,coordinateSpace:'physical',bounds:rect},rect,'win32',convert);
    expect(result).toMatchObject({coordinateSpace:'dip',bounds:{x:-1800/scale,y:300/scale}});
    inputGeometryToDip(result,rect,'win32',convert);expect(convert).toHaveBeenCalledOnce();
  });
  it('leaves Mac coordinates untouched and rejects ambiguous Windows units',()=>{
    const convert=vi.fn();const rect={x:10,y:20,width:1,height:18};
    const mac={available:true,bounds:rect};expect(inputGeometryToDip(mac,rect,'darwin',convert)).toBe(mac);
    expect(inputGeometryToDip(mac,rect,'win32',convert)).toEqual({available:false});expect(convert).not.toHaveBeenCalled();
  });
});
