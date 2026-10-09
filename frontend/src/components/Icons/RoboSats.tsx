import React from 'react';
import { SvgIcon, type SvgIconProps } from '@mui/material';
import { ROBLOSATS_ROBOT } from './RoblosatsRobot';

// Roblosats: the robot instead of RoboSats' logo (same component name, so callers don't change).
const RoboSats: React.FC<SvgIconProps> = (props) => {
  return (
    <SvgIcon {...props} viewBox='0 0 1000 1000'>
      <image href={ROBLOSATS_ROBOT} x='0' y='0' width='1000' height='1000' />
    </SvgIcon>
  );
};

export default RoboSats;
